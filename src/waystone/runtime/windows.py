"""Windows: the bridge's work, done inside Waystone.

Smart App Control and other code integrity policies block unsigned programs, and
the bridge is one. On Windows Waystone therefore protects the session with DPAPI,
computes the device fingerprint, hands the game its credential, and starts or
closes the game itself. Linux keeps running the bridge under Wine. bridge/*.c
stays the reference: each function here names the one it follows.

Only Windows imports this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import time
import winreg

from .bridge import BridgeError
from .game import ASKED, NO_WINDOW, NOT_RUNNING, GameRuntime, RuntimeFailure

ENTROPY = bytes.fromhex('c876f4ae4c952efef2fa0f5419c09c43')
LAUNCH_KEY = r'Software\Blizzard Entertainment\Battle.net\Launch Options\WoW'
IDENTITY_KEY = r'Software\Blizzard Entertainment\Battle.net\Identity'
IDENTITY_MUTEX = 'Local\\wowlauncher.device-identity'
FIELDS = ['RGKY', 'CPGE', 'ULNG', 'SLNG', 'CNME', 'UNME', 'UTCO', 'CARC', 'CREV', 'CLVL', 'PMEM', 'PSZE',
          'OVER', 'CVRA', 'CFTC', 'CFTD', 'CEFC', 'CEFD', 'CBRD', 'CVEN', 'ANME', 'ADSC', 'MAC']
FNV_OFFSET, FNV_PRIME = 0x811c9dc5, 0x1000193
# cpuid(leaf, out): push rbx; mov eax, ecx; mov r8, rdx; xor ecx, ecx; cpuid;
# mov [r8], eax; mov [r8+4], ebx; mov [r8+8], ecx; mov [r8+12], edx; pop rbx; ret
CPUID_CODE = bytes.fromhex('5389c84989d031c90fa241890041895804418948084189500c5bc3')

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
iphlpapi = ctypes.WinDLL('iphlpapi', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')


class Blob(ctypes.Structure):           # DATA_BLOB
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.c_void_p)]


class SystemInfo(ctypes.Structure):
    _fields_ = [('architecture', wintypes.WORD), ('reserved', wintypes.WORD), ('page_size', wintypes.DWORD),
                ('minimum', ctypes.c_void_p), ('maximum', ctypes.c_void_p), ('mask', ctypes.c_size_t),
                ('processors', wintypes.DWORD), ('type', wintypes.DWORD), ('granularity', wintypes.DWORD),
                ('level', wintypes.WORD), ('revision', wintypes.WORD)]


class MemoryStatus(ctypes.Structure):   # MEMORYSTATUSEX
    _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD), ('total_physical', ctypes.c_uint64),
                ('available_physical', ctypes.c_uint64), ('total_page_file', ctypes.c_uint64),
                ('available_page_file', ctypes.c_uint64), ('total_virtual', ctypes.c_uint64),
                ('available_virtual', ctypes.c_uint64), ('available_extended', ctypes.c_uint64)]


class Version(ctypes.Structure):        # OSVERSIONINFOEXW, the service pack kept as raw bytes
    _fields_ = [('size', wintypes.DWORD), ('major', wintypes.DWORD), ('minor', wintypes.DWORD),
                ('build', wintypes.DWORD), ('platform', wintypes.DWORD), ('service_pack', ctypes.c_ubyte * 256),
                ('service_pack_major', wintypes.WORD), ('service_pack_minor', wintypes.WORD),
                ('suite', wintypes.WORD), ('product', wintypes.BYTE), ('reserved', wintypes.BYTE)]


class AdapterInfo(ctypes.Structure):    # the start of IP_ADAPTER_INFO
    _fields_ = [('next', ctypes.c_void_p), ('combo_index', wintypes.DWORD), ('name', ctypes.c_char * 260),
                ('description', ctypes.c_char * 132), ('address_length', wintypes.UINT),
                ('address', ctypes.c_ubyte * 8)]


for _function in (crypt32.CryptProtectData, crypt32.CryptUnprotectData):
    _function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    _function.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualAlloc.restype = ctypes.c_void_p
kernel32.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.ReleaseMutex.argtypes = kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetTimeZoneInformation.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
iphlpapi.GetAdaptersInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
iphlpapi.GetAdaptersInfo.restype = wintypes.ULONG
ntdll.RtlGetVersion.argtypes = [ctypes.POINTER(Version)]
ntdll.RtlGetVersion.restype = ctypes.c_long
WINDOW_CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WINDOW_CALLBACK, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


# DPAPI, as pipe_dpapi

def _crypt(protect: bool, value: bytes, input_limit: int, output_limit: int) -> Blob | None:
    """DPAPI over value with the bridge's entropy, or None. Wipe the result with _release."""
    if not 0 < len(value) <= input_limit:
        return None
    source = ctypes.create_string_buffer(value, len(value))
    entropy = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    output = Blob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    done = function(ctypes.byref(Blob(len(value), ctypes.addressof(source))), None,
                    ctypes.byref(Blob(len(ENTROPY), ctypes.addressof(entropy))), None, None,
                    0x1, ctypes.byref(output))           # CRYPTPROTECT_UI_FORBIDDEN
    ctypes.memset(source, 0, len(value))
    if not done:
        return None
    if not 0 < output.size <= output_limit:
        _release(output)
        return None
    return output


def _release(blob: Blob) -> None:
    ctypes.memset(blob.data, 0, blob.size)
    kernel32.LocalFree(blob.data)


def protect(value: bytes) -> bytes:
    blob = _crypt(True, value, 2048, 8192)
    if blob is None:
        raise BridgeError('Could not protect a new credential')
    try:
        return ctypes.string_at(blob.data, blob.size)
    finally:
        _release(blob)


def unprotect(protected: bytes) -> bytes:
    blob = _crypt(False, protected, 8192, 2048)
    if blob is None:
        raise BridgeError('Could not decrypt the saved credential')
    try:
        return ctypes.string_at(blob.data, blob.size)
    finally:
        _release(blob)


# The device fingerprint, as pipe_device and load_identity

def fnv(data: bytes) -> int:
    value = FNV_OFFSET
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return value


def number(text: str) -> int:
    return fnv(text.encode())


def _read_identity(key) -> bytes | None:
    """The saved 64-byte identity, or None if there is none. A malformed one is never replaced."""
    try:
        value, kind = winreg.QueryValueEx(key, 'Identity')
    except FileNotFoundError:
        return None
    if kind != winreg.REG_BINARY or not isinstance(value, bytes) or len(value) != 64:
        raise BridgeError('The saved device identity is malformed')
    return value


def _identity() -> bytes:
    """The identity seed the game keeps, created once with 64 random bytes."""
    mutex = kernel32.CreateMutexW(None, False, IDENTITY_MUTEX)
    if not mutex:
        raise BridgeError('Could not initialize or read the device fingerprint')
    try:
        if kernel32.WaitForSingleObject(mutex, 30000) not in (0, 0x80):     # signalled or abandoned
            raise BridgeError('Could not initialize or read the device fingerprint')
        try:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IDENTITY_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                    identity = _read_identity(key)
                    if identity is not None:
                        return identity
            except FileNotFoundError:
                pass
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, IDENTITY_KEY, 0,
                                    winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
                # Read again: another program may have written one since.
                identity = _read_identity(key)
                if identity is None:
                    identity = os.urandom(64)
                    winreg.SetValueEx(key, 'Identity', 0, winreg.REG_BINARY, identity)
                    winreg.FlushKey(key)
                return identity
        finally:
            kernel32.ReleaseMutex(mutex)
    finally:
        kernel32.CloseHandle(mutex)


@contextmanager
def _cpuid():
    """A function returning (eax, ebx, ecx, edx) of the CPUID instruction for a leaf."""
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        raise BridgeError('The device fingerprint needs an x86-64 processor')
    size = len(CPUID_CODE)
    address = kernel32.VirtualAlloc(None, size, 0x3000, 0x04)          # MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
    if not address:
        raise BridgeError('Could not initialize or read the device fingerprint')
    try:
        ctypes.memmove(address, CPUID_CODE, size)
        previous = wintypes.DWORD()
        if not kernel32.VirtualProtect(address, size, 0x20, ctypes.byref(previous)):     # PAGE_EXECUTE_READ
            raise BridgeError('Could not initialize or read the device fingerprint')
        kernel32.FlushInstructionCache(kernel32.GetCurrentProcess(), address, size)
        function = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_void_p)(address)

        def cpuid(leaf: int) -> tuple[int, int, int, int]:
            registers = (ctypes.c_uint32 * 4)()
            function(leaf, ctypes.addressof(registers))
            return tuple(registers)
        yield cpuid
    finally:
        kernel32.VirtualFree(address, 0, 0x8000)                         # MEM_RELEASE


def device_id() -> bytes:
    """The game's Device-Id, the same 23 fields as pipe_device in bridge/device_id.c."""
    try:
        return _device_id()
    except OSError:
        raise BridgeError('Could not initialize or read the device fingerprint') from None


def _device_id() -> bytes:
    failed = BridgeError('Could not initialize or read the device fingerprint')
    values = dict.fromkeys(FIELDS, FNV_OFFSET)
    values['RGKY'] = fnv(_identity())
    values['CPGE'] = number(f'{kernel32.GetACP():04X}')
    values['ULNG'] = number(f'{kernel32.GetUserDefaultLangID():04X}')
    values['SLNG'] = number(f'{kernel32.GetSystemDefaultLangID():04X}')
    # Both names are hashed over as many bytes of their UTF-16 text as they have characters.
    name, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
    if not kernel32.GetComputerNameW(name, ctypes.byref(size)):
        raise failed
    values['CNME'] = fnv(ctypes.string_at(name, size.value))
    size = wintypes.DWORD(512)
    if not advapi32.GetUserNameW(name, ctypes.byref(size)) or not size.value:
        raise failed
    values['UNME'] = fnv(ctypes.string_at(name, size.value - 1))      # the size counts the terminator
    zone = ctypes.create_string_buffer(172)                              # TIME_ZONE_INFORMATION
    if kernel32.GetTimeZoneInformation(zone) == 0xFFFFFFFF:
        raise failed
    values['UTCO'] = number(str(-struct.unpack_from('<l', zone.raw)[0]))
    system = SystemInfo()
    kernel32.GetNativeSystemInfo(ctypes.byref(system))
    values['CARC'] = number(str(system.architecture))
    values['CREV'] = number(str(system.revision))
    values['CLVL'] = number(str(system.level))
    memory = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise failed
    values['PMEM'] = number(str(memory.total_physical >> 20))
    values['PSZE'] = number(str(memory.total_page_file >> 20))
    version = Version(size=ctypes.sizeof(Version))
    if ntdll.RtlGetVersion(ctypes.byref(version)):
        raise failed
    # The original prints the wide service pack buffer as narrow text, up to its first zero byte.
    service_pack = bytes(version.service_pack).split(b'\0', 1)[0]
    values['OVER'] = fnv(f'{version.platform}.{version.major}.{version.minor}.{version.build} '.encode()
                         + service_pack)
    with _cpuid() as cpuid:
        maximum, b, c, d = cpuid(0)
        vendor = struct.pack('<III', b, d, c)
        values['CVEN'] = fnv(vendor)
        if maximum >= 1:
            a, _, c, d = cpuid(1)
            values['CVRA'], values['CFTC'], values['CFTD'] = number(f'{a:X}'), number(f'{c:X}'), number(f'{d:X}')
        extended = cpuid(0x80000000)[0]
        if extended >= 0x80000001:
            _, _, c, d = cpuid(0x80000001)
            if vendor == b'GenuineIntel':
                c, d = c & 0xfffffffe, d & 0xfffff7ff
            elif vendor == b'AuthenticAMD':
                c &= 0xfffffffe
            values['CEFC'], values['CEFD'] = number(f'{c:X}'), number(f'{d:X}')
        if extended >= 0x80000004:
            brand = b''.join(struct.pack('<IIII', *cpuid(leaf)) for leaf in (0x80000002, 0x80000003, 0x80000004))
            values['CBRD'] = fnv(brand.split(b'\0', 1)[0])
    size = wintypes.ULONG(0)
    status = iphlpapi.GetAdaptersInfo(None, ctypes.byref(size))
    if status == 111:                                                    # ERROR_BUFFER_OVERFLOW
        buffer = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetAdaptersInfo(buffer, ctypes.byref(size)) == 0:
            adapter = AdapterInfo.from_buffer(buffer)                    # the first adapter
            values['ANME'] = fnv(adapter.name)
            values['ADSC'] = fnv(adapter.description)
            length = min(adapter.address_length, len(adapter.address))
            values['MAC'] = number(''.join(f'{byte:02X}' for byte in adapter.address[:length]))
    return ('{ ' + ', '.join(f'"{field}" : {values[field]}' for field in FIELDS) + ' }').encode()


class Bridge:
    """The bridge's DPAPI and fingerprint requests, answered in this process."""

    def protect(self, value: bytes) -> bytes:
        return protect(value)

    def unprotect(self, protected: bytes) -> bytes:
        return unprotect(protected)

    def device_id(self) -> bytes:
        return device_id()


# The game start, as launch_protected

def _printable(value: str, limit: int) -> bool:
    return 0 < len(value) <= limit and all(32 <= ord(character) < 127 for character in value)


def commit_credential(token: Path, account: tuple[str, str] | None) -> None:
    """Hand the game its protected credential through the registry, with the chosen WoW account.

    The registry gets the DPAPI-protected bytes, as Battle.net writes them. Their
    plaintext is only checked, in memory that is wiped afterwards.
    """
    failed = RuntimeFailure('The game credential could not be handed to the game.')
    if account and not (_printable(account[0], 64) and len(account[1]) == 2
                        and all('A' <= letter <= 'Z' for letter in account[1])):
        raise failed
    try:
        protected = token.read_bytes()
    except OSError:
        raise failed from None
    blob = _crypt(False, protected, 8192, 2048)
    if blob is None:
        raise failed
    try:
        plain = (ctypes.c_ubyte * blob.size).from_address(blob.data)
        if any(byte < 32 or byte >= 127 for byte in plain):
            raise failed
    finally:
        _release(blob)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LAUNCH_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'WEB_TOKEN', 0, winreg.REG_BINARY, protected)
            winreg.SetValueEx(key, 'ACCOUNT_TS', 0, winreg.REG_SZ, str(int(time.time())))
            if account:
                winreg.SetValueEx(key, 'GAME_ACCOUNT', 0, winreg.REG_SZ, account[0])
                winreg.SetValueEx(key, 'REGION', 0, winreg.REG_SZ, account[1])
            else:
                # Without a choice the game asks, so one left from an earlier start goes.
                try:
                    winreg.DeleteValue(key, 'GAME_ACCOUNT')
                except FileNotFoundError:
                    pass
            winreg.FlushKey(key)
    except OSError:
        raise failed from None


def game_command(executable: Path, arguments: list[str]) -> str:
    """The game's command line. Waystone passes plain switches, so a quote is refused."""
    words = [f'"{executable}"']
    for argument in arguments:
        if '"' in argument:
            raise RuntimeFailure(f'A launch argument contains a quote: {argument}')
        words.append(f'"{argument}"' if ' ' in argument else argument)
    return ' '.join(words)


def start_game(executable: Path, arguments: list[str]) -> subprocess.Popen:
    executable = Path(executable).resolve()
    return subprocess.Popen(game_command(executable, arguments), executable=str(executable),
                            cwd=executable.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


# Closing a game, as close_game

def _file_identity(path) -> tuple[int, int]:
    """The volume and file index, which identify the file whatever path names it."""
    status = os.stat(path)
    return status.st_dev, status.st_ino


def _image(handle) -> str | None:
    buffer = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buffer))
    return buffer.value if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None


def _ask_windows_to_close(pid: int) -> int:
    """Post WM_CLOSE to the process's visible top-level windows, as their close button does."""
    asked = 0

    def visit(window, _):
        nonlocal asked
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(window) and not user32.GetWindow(window, 4):  # GW_OWNER
            user32.PostMessageW(window, 0x0010, 0, 0)                     # WM_CLOSE
            asked += 1
        return True
    user32.EnumWindows(WINDOW_CALLBACK(visit), 0)
    return asked


def close_game(executable: Path, processes: list[int], *, force: bool = False) -> str:
    """Ask the processes running executable to close, or end them with force.

    The open process handle keeps its ID from going to a new process, and only a
    process whose image is that very file counts.
    """
    try:
        wanted = _file_identity(executable)
    except OSError:
        return NOT_RUNNING
    running = done = False
    for pid in processes:
        handle = kernel32.OpenProcess(0x1000 | 0x0001, False, pid)      # QUERY_LIMITED_INFORMATION | TERMINATE
        if not handle:
            continue
        try:
            image = _image(handle)
            try:
                if image is None or _file_identity(image) != wanted:
                    continue
            except OSError:
                continue
            running = True
            if force:
                done |= bool(kernel32.TerminateProcess(handle, 1))
            else:
                done |= _ask_windows_to_close(pid) > 0
        finally:
            kernel32.CloseHandle(handle)
    return ASKED if done else NO_WINDOW if running else NOT_RUNNING


class WindowsRuntime(GameRuntime):
    """The game runs natively. The user's own DPAPI keys and registry hold its credential."""

    def __init__(self):
        self.bridge = Bridge()

    def start(self, executable, arguments, token, ack, *, environment=None, wrapper=(), account=None):
        commit_credential(token, account)
        process = start_game(executable, arguments)
        with open(ack, 'x', encoding='utf-8') as handle:
            json.dump({'game_started': True, 'windows_pid': process.pid}, handle)
        return process

    def close(self, executable, processes, *, force=False):
        return close_game(executable, processes, force=force)
