"""Windows does the bridge's work in Scuffle: DPAPI, the device identity, the game's credential and closing it.

These tests write only below a throwaway key under HKCU\\Software, never to Battle.net's own keys.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows only')
if sys.platform == 'win32':
    import winreg
    from scuffle.runtime import windows
    from scuffle.runtime.bridge import BridgeError
    from scuffle.runtime.game import ASKED, NO_WINDOW, NOT_RUNNING, RuntimeFailure


def delete_tree(root, path):
    try:
        with winreg.OpenKey(root, path) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                delete_tree(root, f'{path}\\{child}')
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


@pytest.fixture
def keys(monkeypatch):
    base = f'Software\\Scuffle-tests-{uuid.uuid4().hex}'
    monkeypatch.setattr(windows, 'IDENTITY_KEY', base + '\\Identity')
    monkeypatch.setattr(windows, 'LAUNCH_KEY', base + '\\Launch Options\\WoW')
    yield base
    delete_tree(winreg.HKEY_CURRENT_USER, base)


def values(path):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        result, index = {}, 0
        while True:
            try:
                name, value, kind = winreg.EnumValue(key, index)
            except OSError:
                return result
            result[name] = (value, kind)
            index += 1


def test_the_session_round_trips_through_dpapi_within_the_bridge_limits():
    protected = windows.protect(b'EU-ticket')
    assert protected != b'EU-ticket' and windows.unprotect(protected) == b'EU-ticket'
    with pytest.raises(BridgeError):
        windows.protect(b'x' * 2049)
    with pytest.raises(BridgeError):
        windows.unprotect(b'not protected')


def test_the_device_identity_is_created_once_and_a_malformed_one_is_never_replaced(keys):
    first = windows.device_id()
    fields = json.loads(first)
    assert list(fields) == windows.FIELDS and all(isinstance(value, int) for value in fields.values())
    identity = values(windows.IDENTITY_KEY)['Identity']
    assert identity[1] == winreg.REG_BINARY and len(identity[0]) == 64
    assert windows.device_id() == first and values(windows.IDENTITY_KEY)['Identity'] == identity
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, windows.IDENTITY_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'Identity', 0, winreg.REG_SZ, 'damaged')
    with pytest.raises(BridgeError):
        windows.device_id()
    assert values(windows.IDENTITY_KEY)['Identity'] == ('damaged', winreg.REG_SZ)


# The stand-in game is Python itself, started with -c pass: it needs no window and exits at once.
GAME = Path(sys._base_executable)


def test_the_game_gets_its_protected_credential_and_account_and_starts_without_wrappers(tmp_path, keys):
    token, ack = tmp_path / 'token', tmp_path / 'ack'
    token.write_bytes(windows.protect(b'EU-ticket'))
    runtime = windows.WindowsRuntime()
    process = runtime.start(GAME, ['-c', 'pass'], token, ack,
                            environment={'DXVK_HUD': 'fps'}, wrapper=['gamemoderun'],
                            account=('WOW1', 'EU'))
    assert process.wait(timeout=60) == 0
    assert json.loads(ack.read_text()) == {'game_started': True, 'windows_pid': process.pid}
    launch = values(windows.LAUNCH_KEY)
    assert launch['WEB_TOKEN'] == (token.read_bytes(), winreg.REG_BINARY)
    assert launch['GAME_ACCOUNT'] == ('WOW1', winreg.REG_SZ) and launch['REGION'] == ('EU', winreg.REG_SZ)
    assert abs(int(launch['ACCOUNT_TS'][0]) - time.time()) < 120
    # Without a chosen account the game asks, so the earlier choice goes.
    ack.unlink()
    runtime.start(GAME, ['-c', 'pass'], token, ack).wait(timeout=60)
    assert 'GAME_ACCOUNT' not in values(windows.LAUNCH_KEY)


@pytest.mark.parametrize('plain, arguments, account', [
    (b'EU-ticket\n', [], None),                     # the plaintext must be printable
    (b'EU-ticket', ['-config "a"'], None),          # a quote cannot be passed safely
    (b'EU-ticket', [], ('WOW1', 'E1')),             # a region is two capital letters
])
def test_a_bad_credential_argument_or_account_starts_nothing(tmp_path, keys, plain, arguments, account):
    token, ack = tmp_path / 'token', tmp_path / 'ack'
    token.write_bytes(windows.protect(plain))
    with pytest.raises(RuntimeFailure):
        windows.WindowsRuntime().start(GAME, ['-c', 'pass', *arguments],
                                       token, ack, account=account)
    assert not ack.exists()


def test_closing_finds_the_game_by_its_file_and_force_quit_ends_only_that():
    sleeper = subprocess.Popen([str(GAME), '-c', 'import time; time.sleep(60)'])
    try:
        assert windows.close_game(GAME, []) == NOT_RUNNING
        # A process ID that now runs another program, as after the game exited, is left alone.
        assert windows.close_game(GAME.with_name('pythonw.exe'), [sleeper.pid], force=True) == NOT_RUNNING
        assert sleeper.poll() is None
        assert windows.close_game(GAME, [sleeper.pid]) == NO_WINDOW
        assert windows.close_game(GAME, [sleeper.pid], force=True) == ASKED
        assert sleeper.wait(timeout=30) == 1
    finally:
        sleeper.kill()
