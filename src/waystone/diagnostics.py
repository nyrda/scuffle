"""A plain-text report for bug reports.

It names versions, the runtime, the settings that shape downloads and launches,
the installed clients, the queue and the end of the Proton log. Sign-in data,
account names and ids, the home folder and the user name never appear: the
session files are not read, and the finished text is scrubbed as a whole, so
an error message or a log line cannot carry them in either.

Every part is collected on its own. One that fails says so in a line instead of
stopping the report, so a missing Proton, umu-run, log or library never breaks it.
"""
from __future__ import annotations

from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from . import catalog, system
from .runtime.proton import find_protons, find_umu
from .settings import LOCALE_NAMES, MEGABYTE, REGIONS, Settings, cache_dir, data_dir

LOG_NAME = 'steam-battlenet.log'
LOG_LINES = 10             # the end of the Proton log, where a crash or an exit shows
LOG_TAIL = 64 * 1024       # bytes read from its end, since the log can grow to gigabytes
LINE_LIMIT = 300           # applied after the scrub, so a cut never hides a secret from it
VERSION_TIMEOUT = 5        # seconds umu-run gets to print its version

# Log lines that can carry a ticket or an account are left out entirely.
SECRET_WORDS = re.compile(r'web_token|account_ts|game_account|game-account|\bst=|ticket|token|password|dpapi',
                          re.IGNORECASE)
GENERIC = [
    # Values after names that hold secrets, as in a query, a registry dump or an environment.
    # Prefixed names such as access_token count too.
    (re.compile(r'(?<![A-Za-z0-9])(ST|WEB_TOKEN|ACCOUNT_TS|GAME_ACCOUNT|[\w-]*(?:token|ticket|password|secret))'
                r'(["\']?\s*[=:]\s*["\']?)[^\s"\'&,]+', re.IGNORECASE), r'\1\2<redacted>'),
    (re.compile(r'--game-account\s+\S+(\s+[A-Z]{2}\b)?'), '--game-account <account>'),
    # A query string can hold a ticket. CDN addresses have none, so every one goes.
    (re.compile(r'(\b[a-z][a-z0-9+.-]*://[^\s?#"\']+)\?[^\s"\']*', re.IGNORECASE), r'\1?<redacted>'),
    # The same for a relative address, such as /download?key=value.
    (re.compile(r'(?<=\S)\?(?=[\w.-]+=)[^\s"\']*'), '?<redacted>'),
    # Battle.net tickets look like EU-<hex>-<number>.
    (re.compile(r'\b[A-Z]{2,4}-[0-9A-Fa-f]{8,}-\d{6,}\b'), '<ticket>'),
    (re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+'), '<email>'),
    (re.compile(r'(?<![\w#])[^\W\d_]\w{1,15}#\d{4,6}\b'), '<battletag>'),
]
# Long runs of base64 or hex, such as a DPAPI blob or a token. Paths and CDN keys stay shorter.
BLOB = re.compile(r'[A-Za-z0-9+/=]{64,}|[A-Za-z0-9_-]{64,}')


def report(settings: Settings, library=None, jobs=(), *, updates: dict | None = None,
           qt_platform: str = '') -> str:
    """The whole report. updates maps a client to its last update check, as the window keeps it."""
    sections = [
        ('', lambda: about(qt_platform)),
        ('Runtime', lambda: runtime(settings)),
        ('Settings', lambda: preferences(settings, library)),
        ('Clients', lambda: clients(settings, library, updates)),
        ('Downloads', lambda: queue(jobs)),
        ('Proton log', proton_log),
    ]
    lines = ['Waystone diagnostics',
             'Sign-in data, account names and ids, the home folder and the user name are left out.']
    for title, collect in sections:
        try:
            body = collect()
        except Exception as error:      # one broken part must not cost the rest
            body = [f'Not available ({type(error).__name__})']
        if body is None:                # not on this platform
            continue
        lines.append('')
        if title:
            lines.append(title)
        lines += [f'  {line}' if title else line for line in body]
    text = scrub('\n'.join(lines), private_patterns(settings))
    return '\n'.join(line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT] + '…'
                     for line in text.split('\n')) + '\n'


def command_line_report(root: Path | None) -> str:
    """The same report for `waystone diagnostics`, from the saved settings and queue."""
    try:
        settings = Settings.load()
    except Exception:
        settings = Settings()
    try:
        from . import discovery
        catalog.apply((discovery.load(cache_dir(), settings.region) or {}).get('products'))
    except Exception:
        pass
    library = None
    try:
        folder = root or (Path(settings.root) if settings.root else None)
        if folder is not None:
            from .library import Library
            library = Library(folder)
    except Exception:
        pass
    return report(settings, library, saved_jobs(library))


def saved_jobs(library) -> list:
    """The queue as Waystone last saved it, when it belongs to this folder."""
    from .downloads import Job
    try:
        saved = json.loads((data_dir() / 'downloads.json').read_text())
        if library is None or saved.get('root') != str(library.root):
            return []
        return [Job(row['client'], row['kind'], status=row['status'], error=str(row.get('error') or ''))
                for row in saved['jobs'] if catalog.is_product(row.get('client'))]
    except Exception:
        return []


# Sections

def about(qt_platform: str) -> list[str]:
    lines = [f'Waystone: {attempt(version, "unknown")}, {attempt(installed_as, "unknown")}',
             f'System: {attempt(operating_system, platform.system() or "unknown")}',
             f'Python: {platform.python_version()} ({platform.python_implementation()})']
    qt = attempt(qt_versions, '')
    if qt or qt_platform:
        lines.append(f'Qt: {qt or "unknown"}' + (f', platform {qt_platform}' if qt_platform else ''))
    return lines


def version() -> str:
    """The version from pyproject.toml in a checkout, the executable in the Windows package, or the package."""
    pyproject = Path(__file__).resolve().parents[2] / 'pyproject.toml'
    if not getattr(sys, 'frozen', False) and pyproject.is_file():
        import tomllib
        return tomllib.loads(pyproject.read_text())['project']['version']
    if getattr(sys, 'frozen', False) and system.WINDOWS:
        return windows_file_version(sys.executable)
    from importlib.metadata import version as package_version
    return package_version('waystone')


def windows_file_version(path: str) -> str:
    """ProductVersion from an executable's version resource, which waystone.spec writes."""
    import ctypes
    api = ctypes.WinDLL('version')
    size = api.GetFileVersionInfoSizeW(path, None)
    data = ctypes.create_string_buffer(size)
    value, length = ctypes.c_wchar_p(), ctypes.c_uint()
    if not size or not api.GetFileVersionInfoW(path, 0, size, data) or not api.VerQueryValueW(
            data, r'\StringFileInfo\040904B0\ProductVersion', ctypes.byref(value), ctypes.byref(length)):
        return 'unknown'
    return value.value


def installed_as() -> str:
    if os.environ.get('APPIMAGE'):
        return 'AppImage'
    if getattr(sys, 'frozen', False):
        return 'Windows package' if system.WINDOWS else 'packaged build'
    here = Path(__file__).resolve()
    if here.parts[1:3] == ('nix', 'store'):
        return 'Nix package'
    if (here.parents[2] / 'pyproject.toml').is_file():
        return 'source checkout'
    return 'Python package'


def operating_system() -> str:
    if system.WINDOWS:
        release, build, *_ = platform.win32_ver()
        # Older Pythons call Windows 11 "10". Its builds start at 22000.
        if release == '10' and int(build.split('.')[-1] or 0) >= 22000:
            release = '11'
        return f'Windows {release} ({build})'
    name = attempt(lambda: platform.freedesktop_os_release().get('PRETTY_NAME', ''), '') or platform.system()
    parts = [f'{name} ({platform.system()} {platform.release()})']
    session = os.environ.get('XDG_SESSION_TYPE') or (
        'wayland' if os.environ.get('WAYLAND_DISPLAY') else 'x11' if os.environ.get('DISPLAY') else '')
    if session:
        parts.append({'wayland': 'Wayland', 'x11': 'X11'}.get(session.lower(), session) + ' session')
    if os.environ.get('XDG_CURRENT_DESKTOP'):
        parts.append(os.environ['XDG_CURRENT_DESKTOP'])
    return ', '.join(parts)


def qt_versions() -> str:
    import PySide6
    from PySide6 import QtCore
    return f'{QtCore.qVersion()}, PySide6 {PySide6.__version__}'


def runtime(settings: Settings) -> list[str] | None:
    """Proton, umu-run and the tools around the game. Windows starts the game directly."""
    if system.WINDOWS:
        return None
    from . import launch_options as options
    lines = [proton_line(settings), f'Proton builds found: {attempt(lambda: str(len(find_protons())), "unknown")}',
             umu_line()]
    found = lambda present: 'found' if present else 'not found'
    lines.append(f'GameMode: {found(options.find_program("gamemoderun"))}, '
                 f'MangoHud: {found(options.vulkan_layer("MangoHud") or options.find_program("mangohud"))}')
    if Path('/etc/NIXOS').exists():
        lines.append(f'steam-run: {found(options.find_program("steam-run"))}')
    launch = settings.linux_launch
    on = [label for name, label in [('gamemode', 'GameMode'), ('mangohud', 'MangoHud'), ('wayland', 'Wayland'),
                                    ('hdr', 'HDR'), ('log', 'Proton log')] if getattr(launch, name)]
    lines.append(f'Launch options: {", ".join(on) or "none"}')
    if launch.wrapper.strip():
        # Only the program. Its arguments are the user's own.
        program = attempt(lambda: Path(options.parse_wrapper(launch.wrapper)[0]).name, 'unreadable')
        lines.append(f'Wrapper: {program}, its arguments left out')
    if launch.environment:
        lines.append(f'Environment: {", ".join(launch.environment)}, values left out')
    return lines


def proton_line(settings: Settings) -> str:
    from .launch import LaunchError, select_proton
    try:
        proton = select_proton(settings)
    except LaunchError:
        return 'Proton: none found'
    how = 'chosen' if settings.proton else 'newest found'
    if not proton.path.is_dir():
        return f'Proton: {proton.name}, {how}, missing at {proton.path}'
    return f'Proton: {proton.name}, version {proton_version(proton.path)}, {how}, {proton.path}'


def proton_version(path: Path) -> str:
    """The build name in Proton's version file, which holds a build time and a name."""
    try:
        with (path / 'version').open(errors='replace') as handle:
            words = handle.read(200).split()
        return words[1] if len(words) > 1 else 'unknown'
    except OSError:
        return 'unknown'


def umu_line() -> str:
    umu = find_umu()
    if umu is None:
        return 'umu-run: not found'
    try:
        done = subprocess.run([str(umu), '--version'], capture_output=True, text=True, errors='replace',
                              stdin=subprocess.DEVNULL, timeout=VERSION_TIMEOUT)
        text = next((line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()), '')
    except (OSError, subprocess.SubprocessError):
        text = ''
    # Such as "umu-launcher version 1.4.4 (3.14.8 (main, …) [GCC 16.2.0])". The Python build adds nothing.
    text = text.split(' (', 1)[0][:80]
    return f'umu-run: {text or "version unknown"}, {umu}'


def preferences(settings: Settings, library) -> list[str]:
    if library is None:
        folder = 'not chosen'
    else:
        state = ('a Waystone installation' if library.exists() else
                 'not a Waystone installation' if library.root.exists() else 'missing')
        folder = f'{library.root}, {state}'
        probe = next((p for p in [library.root, *library.root.parents] if p.exists()), None)
        if probe is not None:
            folder += f', {shutil.disk_usage(probe).free / 1e9:.0f} GB free'
    limit = f'{settings.bandwidth_limit // MEGABYTE} MB/s' if settings.bandwidth_limit else 'none'
    yes = lambda value: 'yes' if value else 'no'
    from .auth.vault import Vault
    vault = Vault(data_dir() / 'session', None)
    known = vault.accounts()
    # Whether the files exist and how many accounts the list holds. Their contents stay out.
    sign_in = 'signed in' if vault.has_session() else 'not signed in'
    if known:
        count = len(known['game_accounts'])
        chosen = str(known['owner']) in settings.game_accounts
        sign_in += f', {count} WoW account{"s" if count != 1 else ""}' + (', one chosen' if chosen else '')
    return [f'Folder: {folder}',
            f'Region: {REGIONS.get(settings.region, settings.region)} ({settings.region}), '
            f'language {LOCALE_NAMES.get(settings.locale, settings.locale)} ({settings.locale})',
            f'Connections: {settings.workers}, speed limit {limit}',
            f'Resume downloads on start: {yes(settings.resume_downloads)}, '
            f'pause while playing: {yes(settings.pause_while_playing)}',
            f'Battle.net: {sign_in}']


def clients(settings: Settings, library, updates: dict | None) -> list[str]:
    if library is None or not library.exists():
        return ['None installed']
    lines = []
    removing = library.uninstalling()
    for code in library.installed():
        state = library.state(code) or {}
        parts = [str(state.get('version', 'unknown version')), str(state.get('region', '?')),
                 str(state.get('locale', '?')), update_state(library, code, state, updates)]
        options = settings.client_launch.get(code)
        if options is not None and options.active():
            parts.append('own launch options')
        lines.append(f'{name(code)}: {", ".join(parts)}')
    if removing:
        lines.append(f'{name(removing)}: being uninstalled')
    return lines or ['None installed']


def update_state(library, code: str, state: dict, updates: dict | None) -> str:
    pending = library.pending(code)
    if pending:
        return f'update unfinished ({pending.get("phase", "unknown phase")})'
    checked = (updates or {}).get(code)
    if isinstance(checked, str):
        return f'update available to {checked}'
    if checked is True:
        return 'update needed'
    if checked is False:
        return 'up to date'
    published = catalog.release(code)
    if published and published.version != state.get('version'):
        return f'not checked, {published.version} published'
    return 'not checked'


def queue(jobs) -> list[str]:
    lines = []
    for job in jobs:
        parts = [job.kind, job.status]
        if job.total_bytes:
            parts.append(f'{job.done_bytes / job.total_bytes:.0%}')
        lines.append(f'{name(job.client)}: {", ".join(parts)}')
        if job.error:
            # On one line. The scrub removes paths, addresses and accounts in it, then it is shortened.
            lines.append(f'  Error: {" ".join(job.error.split())}')
    return lines or ['None']


def proton_log() -> list[str] | None:
    """Where the log is and its last lines. Lines that could hold a ticket or an account are left out."""
    if system.WINDOWS:
        return None
    from .launch import log_dir
    path = log_dir() / LOG_NAME
    if not path.is_file():
        return ['No log yet. Turn on Proton log under Settings, Launch, then start a game.']
    info = path.stat()
    written = datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M')
    with path.open('rb') as handle:
        start = max(0, info.st_size - LOG_TAIL)
        handle.seek(start)
        lines = handle.read(LOG_TAIL).decode(errors='replace').splitlines()
    if start:
        lines = lines[1:]           # cut in the middle
    printable = lambda text: ''.join(c if c.isprintable() else ' ' for c in text).strip()
    kept = [printable(line) for line in lines if line.strip() and not SECRET_WORDS.search(line)]
    return [f'File: {path}, {info.st_size / 1e6:.1f} MB, written {written}',
            *(f'| {line}' for line in kept[-LOG_LINES:])]


def name(code: str) -> str:
    return f'{attempt(lambda: catalog.get(code).label, code)} ({code})'


# Privacy

def private_patterns(settings: Settings) -> list[tuple[re.Pattern, str]]:
    """What must not leave the machine: the home folder, the user name, and account names and ids."""
    case = re.IGNORECASE if system.WINDOWS else 0
    homes: set[str] = set()
    for source in [lambda: str(Path.home()), lambda: os.path.realpath(Path.home()),
                   lambda: os.environ.get('HOME', ''), lambda: os.environ.get('USERPROFILE', '')]:
        home = attempt(source, '').rstrip('/\\')
        # A home of / or C:\ would turn every path into ~.
        if len(Path(home).parts) > 1:
            homes |= {home, home.replace('/', '\\'), home.replace('\\', '/')}
    patterns = [(re.compile(re.escape(home) + r'(?![\w.-])', case), '~')
                for home in sorted(homes, key=len, reverse=True)]
    users = {attempt(getpass.getuser, ''), attempt(lambda: Path.home().name, ''),
             *(os.environ.get(variable, '') for variable in ['USER', 'USERNAME', 'LOGNAME'])}
    patterns += [(re.compile(rf'(?<!\w){re.escape(user)}(?!\w)', re.IGNORECASE), '<user>')
                 for user in sorted(users - {''}, key=len, reverse=True)]
    names, ids = account_words(settings)
    patterns += [(re.compile(rf'(?<!\w){re.escape(word)}(?!\w)', re.IGNORECASE), '<account>')
                 for word in sorted(names, key=len, reverse=True)]
    # Whole numbers, also at the end of a sentence, but not a part of a version such as 11.2.5.64253.
    patterns += [(re.compile(rf'(?<!\w)(?<!\d\.){number}(?!\w)(?!\.\d)'), '<id>')
                 for number in sorted(ids, key=len, reverse=True)]
    return patterns


def account_words(settings: Settings) -> tuple[set[str], set[str]]:
    """Every name and id from the saved WoW account list and the account choice, read leniently.

    Only the account list is read. It holds no credentials, and its names must be known
    to remove them from errors and logs.
    """
    names, ids = set(), set()
    for owner, chosen in attempt(lambda: dict(settings.game_accounts), {}).items():
        ids |= {str(owner), str(chosen)}

    def walk(value, key=''):
        if isinstance(value, dict):
            for inner, item in value.items():
                walk(item, inner)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, bool):
            pass
        elif isinstance(value, int) and (key == 'owner' or key.endswith('id')):
            ids.add(str(value))
        elif isinstance(value, str) and value.strip() and key != 'region':
            names.add(value.strip())

    from .auth.vault import Vault
    walk(attempt(lambda: json.loads(Vault(data_dir() / 'session', None).account_list.read_text()), None))
    return names, {number for number in ids if number.lstrip('-').isdigit()}


def scrub(text: str, private: list[tuple[re.Pattern, str]]) -> str:
    # The generic shapes first, so an address keeps none of its parts once a user name in it is replaced.
    for pattern, replacement in GENERIC + private:
        text = pattern.sub(replacement, text)
    # Only runs with a digit, so a long word is never mistaken for a blob.
    return BLOB.sub(lambda m: '<redacted>' if any(c.isdigit() for c in m.group()) else m.group(), text)


def attempt(function, default):
    try:
        return function()
    except Exception:
        return default
