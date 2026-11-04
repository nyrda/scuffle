"""Entries that start one client from the desktop's app launcher, the entry that starts
Scuffle at sign-in, and the command they run.

Every file written here carries a marker that names its client: a key in a desktop entry,
a text chunk in a PNG, the --shortcut argument in a Windows link. The sign-in entry carries
a key of its own on Linux and the --tray argument on Windows. Removal checks the marker, so
an entry Scuffle did not write is never deleted, whatever its name.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib

from . import system
from .library import atomic_bytes
from .settings import data_dir

MARKER = 'X-Scuffle-Client'
AUTOSTART_MARKER = 'X-Scuffle-Autostart'
TRAY_FLAG = '--tray'          # Scuffle's option to start hidden in the tray
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'Scuffle'
ICON_SIZES = (48, 64, 128, 256)
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_KEYWORD = b'Scuffle'


class ShortcutError(OSError):
    pass


# The command

def program() -> list[str]:
    """The command that starts this Scuffle, by a path that survives upgrades.

    An AppImage runs from a temporary mount, so its own file is the stable path. A Nix
    store path changes with every upgrade, so a profile's scuffle that leads to this
    program is preferred to it.
    """
    if os.environ.get('APPIMAGE'):
        return [os.environ['APPIMAGE']]
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    this = Path(sys.argv[0]).absolute()
    if this.name != 'scuffle' or not this.is_file():
        return [sys.executable, '-m', 'scuffle']     # a development run, such as python -m scuffle
    return [str(stable_path(this) or this)]


def stable_path(this: Path) -> Path | None:
    """A scuffle on PATH outside the Nix store that resolves to this one, such as ~/.nix-profile/bin/scuffle."""
    store = Path(os.environ.get('NIX_STORE_DIR') or '/nix/store')
    target = this.resolve()
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        if not folder:
            continue
        candidate = Path(folder).absolute() / 'scuffle'
        try:
            if not candidate.is_relative_to(store) and candidate.is_file() and candidate.resolve() == target:
                return candidate
        except OSError:
            continue
    return None


def shortcut_command(code: str, *, mode: str | None = None, wait: bool = False) -> list[str]:
    """What a shortcut runs. --shortcut also marks a Steam entry or a Windows link as Scuffle's."""
    return [*program(), 'play', code, '--shortcut', *(['--mode', mode] if mode else []),
            *(['--wait'] if wait else [])]


def started_by_shortcut(arguments: list[str], code: str) -> bool:
    """Whether arguments are a shortcut's command for code."""
    for index, word in enumerate(arguments[:-1]):
        if word == 'play' and arguments[index + 1] == code:
            return '--shortcut' in arguments[index + 2:]
    return False


# Markers

def mark_png(data: bytes, code: str) -> bytes:
    """data with a text chunk that names the client, right after the header."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError('Not a PNG image')
    end = 8 + 12 + struct.unpack('>I', data[8:12])[0]
    body = PNG_KEYWORD + b'\0' + code.encode()
    chunk = struct.pack('>I', len(body)) + b'tEXt' + body + struct.pack('>I', zlib.crc32(b'tEXt' + body))
    return data[:end] + chunk + data[end:]


def png_marker(data: bytes) -> str | None:
    """The client a PNG from mark_png names, or None."""
    if not data.startswith(PNG_SIGNATURE):
        return None
    position = 8
    while position + 8 <= len(data):
        length, kind = struct.unpack('>I4s', data[position:position + 8])
        if kind == b'IDAT':
            return None              # the marker sits before the image data
        if kind == b'tEXt':
            keyword, _, value = data[position + 8:position + 8 + length].partition(b'\0')
            if keyword == PNG_KEYWORD:
                return value.decode(errors='replace')
        position += 12 + length
    return None


def file_marker(path: Path) -> str | None:
    try:
        with path.open('rb') as handle:
            return png_marker(handle.read(65536))
    except OSError:
        return None


def write_marked_png(path: Path, data: bytes, code: str) -> bool:
    """Write the image unless a file there is someone else's. True when written."""
    if path.exists() and file_marker(path) != code:
        return False
    atomic_bytes(path, mark_png(data, code))
    return True


def remove_marked(path: Path, code: str) -> bool:
    if path.is_file() and not path.is_symlink() and file_marker(path) == code:
        path.unlink()
        return True
    return False


# Linux desktop entries

def applications_dir() -> Path:
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'applications'


def icon_paths(code: str) -> list[Path]:
    theme = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'icons' / 'hicolor'
    return [theme / f'{size}x{size}' / 'apps' / f'scuffle-{code}.png' for size in ICON_SIZES]


def desktop_path(code: str) -> Path:
    return applications_dir() / f'scuffle-{code}.desktop'


# Characters that need quoting in an Exec argument, by the desktop entry specification.
RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def exec_argument(argument: str) -> str:
    argument = argument.replace('%', '%%')
    if argument and not RESERVED & set(argument):
        return argument
    return '"' + ''.join('\\' + c if c in '"`$\\' else c for c in argument) + '"'


def exec_line(command: list[str]) -> str:
    # Exec is a string value, whose own escapes apply before the quoting rules.
    return ' '.join(map(exec_argument, command)).replace('\\', '\\\\')


def entry_value(text: str) -> str:
    return text.replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')


def desktop_entry(client, command: list[str]) -> str:
    """One entry per client. A client with game modes gets an action per mode, and the
    entry itself starts the mode last chosen in Scuffle."""
    modes = [mode for mode in client.modes if re.fullmatch(r'[A-Za-z0-9-]+', mode.id)]
    lines = ['[Desktop Entry]', 'Type=Application', 'Version=1.5', f'Name={entry_value(client.label)}',
             f'Comment=Start {entry_value(client.label)} with Scuffle', f'Exec={exec_line(command)}',
             f'Icon=scuffle-{client.code}', 'Terminal=false', 'Categories=Game;', f'{MARKER}={client.code}']
    if Path(command[0]).is_absolute():
        lines.insert(6, f'TryExec={entry_value(command[0])}')    # hidden once the program is gone
    if modes:
        lines.append('Actions=' + ''.join(f'{mode.id};' for mode in modes))
    for mode in modes:
        lines += ['', f'[Desktop Action {mode.id}]', f'Name={entry_value(mode.label)}',
                  f'Exec={exec_line([*command, "--mode", mode.id])}']
    return '\n'.join(lines) + '\n'


def desktop_marker(path: Path, key: str = MARKER) -> str | None:
    try:
        text = path.read_text(errors='replace')
    except OSError:
        return None
    found = re.search(rf'^{re.escape(key)}=(.*)$', text, re.MULTILINE)
    return found.group(1).strip() if found else None


# Windows Start menu links

def start_menu_dir() -> Path:
    appdata = Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming')
    return appdata / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Scuffle'


def icon_file(code: str) -> Path:
    return data_dir() / 'shortcuts' / f'{code}.ico'


def link_strings(data: bytes) -> dict[str, str]:
    """The strings of a Windows shell link (MS-SHLLINK): name, relative path, working
    directory, arguments and icon location, as far as the link has them."""
    if len(data) < 76 or struct.unpack('<I', data[:4])[0] != 0x4C:
        raise ValueError('Not a shell link')
    flags = struct.unpack('<I', data[20:24])[0]
    position = 76
    if flags & 0x1:                              # HasLinkTargetIDList
        position += 2 + struct.unpack('<H', data[position:position + 2])[0]
    if flags & 0x2:                              # HasLinkInfo
        position += struct.unpack('<I', data[position:position + 4])[0]
    unicode = bool(flags & 0x80)
    strings = {}
    for bit, name in [(0x4, 'name'), (0x8, 'relative_path'), (0x10, 'working_dir'), (0x20, 'arguments'),
                      (0x40, 'icon_location')]:
        if not flags & bit:
            continue
        count = struct.unpack('<H', data[position:position + 2])[0]
        size = count * 2 if unicode else count
        raw = data[position + 2:position + 2 + size]
        if len(raw) != size:
            raise ValueError('The shell link ends early')
        strings[name] = raw.decode('utf-16-le' if unicode else 'mbcs' if system.WINDOWS else 'latin-1')
        position += 2 + size
    return strings


def link_marker(path: Path, code: str) -> bool:
    try:
        arguments = link_strings(path.read_bytes()).get('arguments', '')
    except (OSError, ValueError):
        return False
    return started_by_shortcut(arguments.split(), code)


def own_links(code: str) -> list[Path]:
    folder = start_menu_dir()
    if not folder.is_dir():
        return []
    return [path for path in folder.glob('*.lnk') if not path.is_symlink() and link_marker(path, code)]


def ico(pngs: dict[int, bytes]) -> bytes:
    """An icon file with one PNG image per size."""
    sizes = sorted(pngs)
    offset = 6 + 16 * len(sizes)
    header = struct.pack('<HHH', 0, 1, len(sizes))
    for size in sizes:
        # A width or height of 0 means 256.
        header += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(pngs[size]), offset)
        offset += len(pngs[size])
    return header + b''.join(pngs[size] for size in sizes)


def ico_marker(path: Path) -> str | None:
    """The marker of an icon file's first image."""
    try:
        data = path.read_bytes()
        offset = struct.unpack('<I', data[18:22])[0]
    except (OSError, struct.error):
        return None
    return png_marker(data[offset:])


def create_link(path: Path, target: str, arguments: str, icon: Path, description: str) -> None:
    """A .lnk through Windows' own shell object, written beside the final name and then renamed into place."""
    temporary = path.with_name(f'.scuffle-{os.getpid()}.lnk')
    script = ('$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SCUFFLE_LINK); '
              '$link.TargetPath = $env:SCUFFLE_TARGET; $link.Arguments = $env:SCUFFLE_ARGUMENTS; '
              '$link.WorkingDirectory = $env:SCUFFLE_FOLDER; $link.IconLocation = $env:SCUFFLE_ICON; '
              '$link.Description = $env:SCUFFLE_DESCRIPTION; $link.Save()')
    # Values travel in the environment, so no quoting can change the script.
    environment = os.environ | {'SCUFFLE_LINK': str(temporary), 'SCUFFLE_TARGET': target,
                                'SCUFFLE_ARGUMENTS': arguments, 'SCUFFLE_FOLDER': str(Path(target).parent),
                                'SCUFFLE_ICON': f'{icon},0', 'SCUFFLE_DESCRIPTION': description}
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                env=environment, capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0 or not temporary.is_file():
            raise ShortcutError(f'Windows could not create the shortcut. {result.stderr.strip()}'.strip())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def link_name(label: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', label).strip(' .') or 'World of Warcraft'


# The app launcher, on either system

def in_launcher(code: str) -> bool:
    if system.WINDOWS:
        return bool(own_links(code))
    return desktop_marker(desktop_path(code)) == code


def add_to_launcher(client, icons: dict[int, bytes]) -> None:
    """Add or renew client's entry. icons maps pixel sizes to PNG images of its tile."""
    code = client.code
    command = shortcut_command(code)
    if system.WINDOWS:
        icon = icon_file(code)
        if icon.exists() and ico_marker(icon) != code:
            raise ShortcutError(f'{icon} was not made by Scuffle')
        atomic_bytes(icon, ico({size: mark_png(data, code) for size, data in icons.items()}))
        for old in own_links(code):
            old.unlink()
        path = start_menu_dir() / f'{link_name(client.label)}.lnk'
        if path.exists():
            raise ShortcutError(f'{path.name} in the Start menu was not made by Scuffle')
        path.parent.mkdir(parents=True, exist_ok=True)
        create_link(path, command[0], subprocess.list2cmdline(command[1:]), icon,
                    f'Start {client.label} with Scuffle')
        return
    path = desktop_path(code)
    if path.exists() and desktop_marker(path) != code:
        raise ShortcutError(f'{path} was not made by Scuffle')
    for target in icon_paths(code):
        size = int(target.parent.parent.name.split('x')[0])
        if size in icons:
            write_marked_png(target, icons[size], code)
    atomic_bytes(path, desktop_entry(client, command).encode())


def remove_from_launcher(code: str) -> bool:
    """Remove the entry and icons Scuffle wrote for code. True when there was an entry."""
    if system.WINDOWS:
        links = own_links(code)
        for path in links:
            path.unlink()
        icon = icon_file(code)
        if icon.is_file() and ico_marker(icon) == code:
            icon.unlink()
        try:
            start_menu_dir().rmdir()        # only when no other client's link is left
        except OSError:
            pass
        return bool(links)
    path = desktop_path(code)
    found = desktop_marker(path) == code and not path.is_symlink()
    if found:
        path.unlink()
    for icon in icon_paths(code):
        remove_marked(icon, code)
    return found


# Starting Scuffle at sign-in

def tray_command() -> list[str]:
    return [*program(), TRAY_FLAG]


def autostart_path() -> Path:
    """The desktop's autostart entry, by the XDG autostart specification."""
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'autostart' / 'scuffle.desktop'


def autostart_entry(command: list[str]) -> str:
    lines = ['[Desktop Entry]', 'Type=Application', 'Version=1.5', 'Name=Scuffle',
             'Comment=Keep World of Warcraft up to date from the tray', f'Exec={exec_line(command)}',
             'Icon=scuffle', 'Terminal=false', 'X-GNOME-Autostart-enabled=true', f'{AUTOSTART_MARKER}=true']
    if Path(command[0]).is_absolute():
        lines.insert(6, f'TryExec={entry_value(command[0])}')    # nothing starts once the program is gone
    return '\n'.join(lines) + '\n'


def own_run_value(value) -> bool:
    """Whether a Run value is the command Scuffle writes, by its --tray argument."""
    return isinstance(value, str) and value.split()[-1:] == [TRAY_FLAG]


def _run_value():
    """The Run value named Scuffle, or None."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_VALUE)[0]
    except FileNotFoundError:
        return None


def starts_at_sign_in() -> bool:
    """Whether Scuffle's own entry starts it at sign-in."""
    if system.WINDOWS:
        return own_run_value(_run_value())
    path = autostart_path()
    return not path.is_symlink() and desktop_marker(path, AUTOSTART_MARKER) == 'true'


def start_at_sign_in(on: bool) -> None:
    """Write or remove the entry. An entry of that name that Scuffle did not make stays as it is."""
    if system.WINDOWS:
        _set_run_value(on)
        return
    path = autostart_path()
    ours = starts_at_sign_in()
    if not on:
        if ours:
            path.unlink()
        return
    if (path.exists() or path.is_symlink()) and not ours:
        raise ShortcutError(f'{path} was not made by Scuffle, so it was left as it is.')
    atomic_bytes(path, autostart_entry(tray_command()).encode())


def _set_run_value(on: bool) -> None:
    import winreg
    value = _run_value()
    if not on and not own_run_value(value):
        return                      # nothing of Scuffle's to remove
    if on and value is not None and not own_run_value(value):
        raise ShortcutError(f'The start entry named {RUN_VALUE} was not made by Scuffle, so it was left as it is.')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(tray_command()))
        else:
            winreg.DeleteValue(key, RUN_VALUE)
