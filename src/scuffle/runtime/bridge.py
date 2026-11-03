"""Run the Windows bridge for DPAPI, the device fingerprint and game start.

On Linux it runs inside Scuffle's Wine prefix with Proton's Wine binary. On
Windows Scuffle does the same work itself, in runtime.windows.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from .. import system
from ..library import atomic_bytes
from ..settings import data_dir

FRAME_MAGIC = b'WBC1'


class BridgeError(RuntimeError):
    pass


def read_frame(output: bytes, limit: int) -> bytes | None:
    """Return the payload of a bridge frame, or None if it is malformed."""
    if len(output) < 8 or output[:4] != FRAME_MAGIC:
        return None
    size = struct.unpack_from('<I', output, 4)[0]
    if not 0 < size <= limit or len(output) != size + 8:
        return None
    return output[8:]


def windows_path(path: Path) -> str:
    """The path as Windows programs see it: unchanged on Windows, Z: under Wine."""
    path = Path(path).resolve()
    return str(path) if system.WINDOWS else 'Z:' + str(path).replace('/', '\\')


def bridge_executable() -> Path:
    """The bridge a package points to, a lasting copy of the packaged one, or the source tree's."""
    override = os.environ.get('SCUFFLE_BRIDGE')
    if override:
        bridge = Path(override)
        # The AppImage points into its own mount, which goes away when Scuffle exits.
        # The Nix store stays, so the Nix package's bridge runs where it is.
        appdir = os.environ.get('APPDIR')
        if appdir and bridge.is_relative_to(appdir) and bridge.is_file():
            return installed_copy(bridge, data_dir() / 'bin')
        return bridge
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle is None:
        return Path(__file__).parents[1] / 'bridge' / 'wow_auth_bridge.exe'
    packaged = Path(bundle) / 'bridge' / 'wow_auth_bridge.exe'
    return installed_copy(packaged, data_dir() / 'bin') if packaged.is_file() else packaged


def installed_copy(packaged: Path, folder: Path) -> Path:
    """A copy of the packaged bridge that outlives this Scuffle.

    The one-file Windows build unpacks into a temporary folder that is deleted when
    Scuffle exits, and an AppImage unmounts. The bridge waits for the game, so it runs
    from here. The name carries the content hash, so a newer Scuffle never replaces a
    bridge that a running game still uses.
    """
    data = packaged.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = folder / f'{packaged.stem}-{digest}{packaged.suffix}'
    try:
        if target.read_bytes() == data:
            return target
    except FileNotFoundError:
        pass
    atomic_bytes(target, data)
    for old in folder.glob(f'{packaged.stem}-*{packaged.suffix}'):
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass        # Windows keeps a running bridge. The next start removes it.
    return target


class Bridge:
    """Shared request handling. Subclasses decide how the executable runs."""

    def __init__(self, executable: Path):
        self.executable = Path(executable)

    def command(self, arguments: list[str]) -> tuple[list[str], dict]:
        raise NotImplementedError

    def finish(self) -> None:
        """Wait until the bridge's runtime released the prefix."""

    def run(self, arguments: list[str], *, data: bytes | None = None, timeout: float = 120):
        command, env = self.command(arguments)
        try:
            return subprocess.run(command, env=env, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout)
        finally:
            self.finish()

    def _frame(self, mode: str, value: bytes, limit: int, error: str) -> bytes:
        result = self.run([mode], data=struct.pack('<I', len(value)) + value)
        payload = None if result.returncode else read_frame(result.stdout, limit)
        if payload is None:
            raise BridgeError(error)
        return payload

    def protect(self, value: bytes) -> bytes:
        return self._frame('--protect-auth-stdin', value, 8192, 'Could not protect a new credential')

    def unprotect(self, protected: bytes) -> bytes:
        return self._frame('--unprotect-auth-stdin', protected, 2048, 'Could not decrypt the saved credential')

    def device_id(self) -> bytes:
        result = self.run(['--pipe-device'])
        if result.returncode:
            raise BridgeError('Could not initialize or read the device fingerprint')
        data = result.stdout.strip()
        parsed = json.loads(data)
        if len(parsed) != 23 or any(not isinstance(v, int) for v in parsed.values()):
            raise BridgeError('Unexpected device fingerprint fields')
        return data


class WineBridge(Bridge):
    """Linux: the bridge runs with Proton's Wine against Scuffle's prefix.

    UMU does not forward stdin to Windows programs, so short bridge calls use
    Wine directly. On NixOS, Proton's Wine needs an FHS environment such as
    steam-run.
    """

    def __init__(self, executable: Path, prefix: Path, wine: list[str]):
        super().__init__(executable)
        self.prefix, self.wine = Path(prefix), wine

    def environment(self) -> dict:
        return os.environ | {'WINEPREFIX': str(self.prefix), 'WINEDEBUG': '-all', 'WOW_BROKER_PIPE': '1'}

    def command(self, arguments):
        return [*self.wine, str(self.executable), *arguments], self.environment()

    def run(self, arguments, *, data=None, timeout=120):
        # Separate Wine servers can share a prefix. Serialize bridge calls so a
        # first-run identity is created once.
        with system.locked_file(self.prefix / '.scuffle-bridge.lock'):
            return super().run(arguments, data=data, timeout=timeout)

    def finish(self):
        # Let this Wine server save the registry and exit before another
        # runtime, such as the game's UMU container, opens the prefix.
        wineserver = Path(self.wine[-1]).with_name('wineserver')
        if wineserver.is_file():
            subprocess.run([*self.wine[:-1], str(wineserver), '-w'], env=self.environment(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
