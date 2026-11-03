"""Non-Steam game entries in Steam's shortcuts.vdf, so Steam starts a client through Scuffle.

The file is binary VDF. Scuffle keeps every field it reads as raw bytes, so entries it
does not change are written back byte for byte, whatever fields a Steam version adds.
Steam keeps the list in memory and writes it back while it runs, so Scuffle only
changes the file while Steam is closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import stat
import struct
import zlib

from . import system
from .library import atomic_bytes
from .shortcuts import remove_marked, started_by_shortcut, write_marked_png

MAP, STRING, INT32, FLOAT32, POINTER, WIDE_STRING, COLOR, UINT64, END, INT64 = 0, 1, 2, 3, 4, 5, 6, 7, 8, 10
FIXED = {INT32: 4, FLOAT32: 4, POINTER: 4, COLOR: 4, UINT64: 8, INT64: 8}
EMPTY = b'\x00shortcuts\x00\x08\x08'
BACKUP = 'shortcuts.vdf.scuffle-backup'
# Steam names grid images by the entry's app ID: p for the portrait capsule, none for the wide one.
GRID = {'portrait': '{}p.png', 'wide': '{}.png', 'icon': '{}_icon.png'}


class VdfError(ValueError):
    pass


class SteamRunning(OSError):
    pass


# A map is a list of (type, key, value). Keys and strings are bytes without their end,
# numbers stay raw bytes and maps nest, so serialize(parse(data)) == data.

def parse(data: bytes) -> list:
    items, position = _map(data, 0)
    if position != len(data):
        raise VdfError('Steam\'s shortcuts file has data after its end')
    return items


def _text(data: bytes, position: int) -> tuple[bytes, int]:
    end = data.find(b'\0', position)
    if end < 0:
        raise VdfError('Steam\'s shortcuts file ends inside a text')
    return data[position:end], end + 1


def _wide(data: bytes, position: int) -> tuple[bytes, int]:
    end = position
    while data[end:end + 2] != b'\0\0':
        if end + 2 > len(data):
            raise VdfError('Steam\'s shortcuts file ends inside a text')
        end += 2
    return data[position:end], end + 2


def _map(data: bytes, position: int) -> tuple[list, int]:
    items = []
    while True:
        if position >= len(data):
            raise VdfError('Steam\'s shortcuts file ends inside an entry')
        kind = data[position]
        position += 1
        if kind == END:
            return items, position
        key, position = _text(data, position)
        if kind == MAP:
            value, position = _map(data, position)
        elif kind == STRING:
            value, position = _text(data, position)
        elif kind == WIDE_STRING:
            value, position = _wide(data, position)
        elif kind in FIXED:
            value = data[position:position + FIXED[kind]]
            if len(value) != FIXED[kind]:
                raise VdfError('Steam\'s shortcuts file ends inside a number')
            position += FIXED[kind]
        else:
            # Its length is unknown, so nothing after it could be read or written back safely.
            raise VdfError(f'Steam\'s shortcuts file has a field of a type Scuffle does not know ({kind})')
        items.append((kind, key, value))


def serialize(items: list) -> bytes:
    out = bytearray()
    for kind, key, value in items:
        out += bytes([kind]) + key + b'\0'
        if kind == MAP:
            out += serialize(value)
        elif kind == STRING:
            out += value + b'\0'
        elif kind == WIDE_STRING:
            out += value + b'\0\0'
        else:
            out += value
    return bytes(out + bytes([END]))


def field(entry: list, name: str):
    """A field's value. Steam has written both AppName and appname over the years."""
    wanted = name.lower().encode()
    return next((value for _, key, value in entry if key.lower() == wanted), None)


def shortcut_list(items: list) -> list:
    found = next((value for kind, key, value in items if kind == MAP and key.lower() == b'shortcuts'), None)
    if found is None:
        raise VdfError('Steam\'s shortcuts file has no list of shortcuts')
    return found


def ours(entry: list, code: str) -> bool:
    options = field(entry, 'LaunchOptions')
    if not isinstance(options, bytes):
        return False
    try:
        words = shlex.split(options.decode(errors='replace'))
    except ValueError:
        return False
    return started_by_shortcut(words, code)


def app_id(exe: str, name: str) -> int:
    """The ID Steam derives for a non-Steam game, which also names its grid images."""
    return zlib.crc32((exe + name).encode()) | 0x80000000


def quote(path: str) -> str:
    return f'"{path}"'


def new_entry(index: int, appid: int, name: str, command: list[str], icon: str) -> tuple:
    """An entry with the fields, in the order, Steam writes for a non-Steam game it adds itself."""
    def string(key, value): return (STRING, key.encode(), value.encode())
    def number(key, value): return (INT32, key.encode(), struct.pack('<i', value))
    exe = command[0]
    fields = [(INT32, b'appid', struct.pack('<I', appid)), string('AppName', name), string('Exe', quote(exe)),
              string('StartDir', quote(str(Path(exe).parent))), string('icon', icon), string('ShortcutPath', ''),
              string('LaunchOptions', shlex.join(command[1:])), number('IsHidden', 0),
              number('AllowDesktopConfig', 1), number('AllowOverlay', 1), number('OpenVR', 0), number('Devkit', 0),
              string('DevkitGameID', ''), number('DevkitOverrideAppID', 0), number('LastPlayTime', 0),
              string('FlatpakAppID', ''), string('sortas', ''), (MAP, b'tags', [])]
    return (MAP, str(index).encode(), fields)


def entry_app_id(entry: list) -> int | None:
    value = field(entry, 'appid')
    return struct.unpack('<I', value)[0] if isinstance(value, bytes) and len(value) == 4 else None


@dataclass
class Steam:
    root: Path

    def users(self) -> list[Path]:
        """The config folder of every Steam account that signed in here.

        An account folder that cannot be read raises, so a change never skips it unnoticed.
        """
        folder = self.root / 'userdata'
        if not folder.is_dir():
            return []
        found = []
        for user in folder.iterdir():
            if not (user.name.isdigit() and int(user.name) > 0):
                continue
            try:
                if stat.S_ISDIR((user / 'config').stat().st_mode):
                    found.append(user / 'config')
            except (FileNotFoundError, NotADirectoryError):
                continue
        return sorted(found)

    def running(self) -> bool:
        """Whether this Steam installation's client runs."""
        root = self.root.resolve()

        def inside(path: Path, name: str) -> bool:
            try:
                return path.name.lower() == name and path.resolve().is_relative_to(root)
            except OSError:
                return False

        if system.WINDOWS:
            return any(inside(Path(image), 'steam.exe') for _, image, _ in system.running_executables())
        for entry in Path('/proc').glob('[0-9]*'):
            paths = []
            try:
                first = (entry / 'cmdline').read_bytes().split(b'\0', 1)[0].decode(errors='replace')
                if first.startswith('/'):
                    paths.append(Path(first))
                paths.append(Path(os.readlink(entry / 'exe')))
            except OSError:
                pass
            if any(inside(path, 'steam') for path in paths):
                return True
        return False

    def has(self, code: str) -> bool:
        try:
            users = self.users()
        except OSError:
            return False
        for config in users:
            try:
                if any(ours(value, code) for _, _, value in shortcut_list(parse(self._read(config)))):
                    return True
            except (OSError, VdfError):
                continue
        return False

    @staticmethod
    def _read(config: Path) -> bytes:
        try:
            return (config / 'shortcuts.vdf').read_bytes()
        except FileNotFoundError:
            return EMPTY

    def add(self, code: str, name: str, command: list[str], images: dict[str, bytes]) -> None:
        """Add the client for every Steam account, replacing an entry Scuffle added before.

        images maps 'portrait', 'wide' and 'icon' to PNG images.
        """
        appid = app_id(quote(command[0]), name)

        def entry(config: Path, index: int) -> tuple:
            icon = config / 'grid' / GRID['icon'].format(appid) if 'icon' in images else ''
            return new_entry(index, appid, name, command, str(icon))

        def artwork(config: Path) -> None:
            for kind, pattern in GRID.items():
                if kind in images:
                    # Artwork the user chose in Steam has no marker and stays.
                    write_marked_png(config / 'grid' / pattern.format(appid), images[kind], code)

        self.change(code, entry, artwork)

    def remove(self, code: str) -> bool:
        """Remove the entries Scuffle added for the client. True when there were any."""
        return self.change(code)

    def change(self, code: str, entry=None, artwork=None) -> bool:
        """Replace the client's entries in every account's file with entry's, or remove them."""
        if self.running():
            raise SteamRunning('Steam is running')
        # Every file is read and checked before the first one is written.
        plans = []
        for config in self.users():
            data = self._read(config)
            items = parse(data)
            if serialize(items) != data:
                raise VdfError('Scuffle cannot write Steam\'s shortcuts file back unchanged, so it wrote nothing')
            entries = shortcut_list(items)
            kept = [item for item in entries if not ours(item[2], code)]
            removed = [item for item in entries if ours(item[2], code)]
            index = max((int(key) for _, key, _ in kept if key.isdigit()), default=-1) + 1
            added = [entry(config, index)] if entry else []
            if not removed and not added:
                continue
            entries[:] = kept + added
            new = serialize(items)
            check(data, new, kept)
            plans.append((config, new, removed))
        for config, new, removed in plans:
            if artwork:
                artwork(config)
            path = config / 'shortcuts.vdf'
            mode = 0o644
            if path.exists():
                mode = path.stat().st_mode & 0o777
                shutil.copy2(path, config / BACKUP)
            atomic_bytes(path, new)
            os.chmod(path, mode)
            left = {entry_app_id(item[2]) for item in shortcut_list(parse(new))}
            for appid in {entry_app_id(item[2]) for item in removed} - left - {None}:
                for pattern in GRID.values():
                    remove_marked(config / 'grid' / pattern.format(appid), code)
        return any(removed for *_, removed in plans)


def check(old: bytes, new: bytes, kept: list) -> None:
    """Refuse to write unless every entry kept is in the new file unchanged."""
    entries = shortcut_list(parse(new))
    if [serialize([item]) for item in entries[:len(kept)]] != [serialize([item]) for item in kept]:
        raise VdfError('Scuffle would have changed other Steam shortcuts, so it wrote nothing')
    others = [item for item in parse(old) if not (item[0] == MAP and item[1].lower() == b'shortcuts')]
    if [item for item in parse(new) if not (item[0] == MAP and item[1].lower() == b'shortcuts')] != others:
        raise VdfError('Scuffle would have changed Steam\'s shortcuts file, so it wrote nothing')


def find() -> Steam | None:
    """The Steam installation with at least one account. Flatpak's Steam cannot start
    programs outside its sandbox, so it is not looked for."""
    if system.WINDOWS:
        candidates = []
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                candidates.append(Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
        except OSError:
            pass
        candidates.append(Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam')
    else:
        home = Path.home()
        data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share')
        candidates = [home / '.steam/steam', home / '.steam/root', data / 'Steam', home / '.local/share/Steam']
    for candidate in candidates:
        try:
            steam = Steam(candidate.resolve())
            if steam.users():
                return steam
        except OSError:
            continue
    return None
