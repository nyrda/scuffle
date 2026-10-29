"""Operating-system differences: file locks, directory sync and processes."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys

WINDOWS = sys.platform == 'win32'

if WINDOWS:
    import msvcrt
else:
    import fcntl

# Optional hardening flags that Windows does not define.
O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
O_BINARY = getattr(os, 'O_BINARY', 0)


class LockBusy(OSError):
    pass


def lock(handle, *, blocking: bool = True) -> None:
    """Take an exclusive lock on an open file, or raise LockBusy."""
    if WINDOWS:
        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError:
            raise LockBusy('locked by another process') from None
        return
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        raise LockBusy('locked by another process') from None


def unlock(handle) -> None:
    if WINDOWS:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_file(path: Path, *, blocking: bool = True):
    """Hold an exclusive lock on path for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        lock(handle, blocking=blocking)
        try:
            yield handle
        finally:
            unlock(handle)


def sync_directory(path: Path) -> None:
    """Make a rename or creation inside path durable.

    Windows has no directory handle to flush. NTFS journals the rename itself.
    """
    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def running_executables(names: set[str] | None = None) -> list[tuple[int, str, str]]:
    """List running processes as (pid, executable path or command line, cwd).

    With names, only processes whose command line contains one of them, in lower case,
    which saves resolving every other process's folder.
    """
    if WINDOWS:
        return _windows_processes()
    result = []
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if names is not None and not any(name in command.lower() for name in names):
                continue
            try:
                cwd = str((entry / 'cwd').resolve())
            except OSError:
                cwd = ''
            result.append((int(entry.name), command, cwd))
        except OSError:
            continue
    return result


def game_processes(executable: Path) -> list[int]:
    """The processes running executable itself, not the launchers whose arguments name it.

    Under Wine the process shows its Windows path, under whichever drive letter it was
    started, so its name and folder identify it.
    """
    if WINDOWS:
        return [pid for pid, image, _ in _windows_processes()
                if Path(image).resolve() == Path(executable).resolve()]
    return [int(entry.name) for entry in Path('/proc').glob('[0-9]*') if runs(int(entry.name), executable)]


def runs(pid: int, executable: Path) -> bool:
    """Whether process pid runs executable itself, by its first argument and its folder. Linux only."""
    entry = Path(f'/proc/{pid}')
    try:
        first = (entry / 'cmdline').read_bytes().split(b'\0', 1)[0].decode(errors='replace')
        if first.replace('\\', '/').rsplit('/', 1)[-1].lower() != executable.name.lower():
            return False
        return Path(first) == executable or Path((entry / 'cwd').resolve()) == Path(executable).parent.resolve()
    except OSError:
        return False


def _windows_processes() -> list[tuple[int, str, str]]:
    import ctypes
    from ctypes import wintypes
    psapi, kernel32 = ctypes.WinDLL('psapi'), ctypes.WinDLL('kernel32')
    ids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)):
        return []
    result = []
    for pid in ids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                result.append((pid, buffer.value, ''))
        finally:
            kernel32.CloseHandle(handle)
    return result
