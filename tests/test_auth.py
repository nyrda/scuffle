"""Check the bridge frame contract, the packaged bridge, pasted sign-in tickets and session renewal."""
from pathlib import Path
import struct
import sys

import pytest

from waystone import launch
from waystone.auth.bgs import CredentialRejected
from waystone.auth.vault import Vault
from waystone.launch import Launcher, looks_like_redirect, parse_ticket
from waystone.runtime.bridge import bridge_executable, read_frame
from waystone.settings import Settings, data_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('output', [
    b'',
    b'WBC1\x03\x00\x00',
    b'XXXX' + struct.pack('<I', 3) + b'abc',
    b'WBC1' + struct.pack('<I', 0),
    b'WBC1' + struct.pack('<I', 4) + b'abc',
    b'WBC1' + struct.pack('<I', 2) + b'abc',
    b'WBC1' + struct.pack('<I', 9) + b'x' * 9,
])
def test_malformed_bridge_frame_is_rejected(output):
    assert read_frame(output, 8) is None


def test_bridge_frame_payload_is_returned():
    assert read_frame(b'WBC1' + struct.pack('<I', 3) + b'abc', 8) == b'abc'


def test_a_packaged_bridge_runs_from_a_lasting_copy_that_an_update_never_overwrites(tmp_path, monkeypatch):
    # A one-file build unpacks into a temporary folder that goes away when Waystone exits,
    # while the bridge keeps waiting for the game.
    monkeypatch.delenv('WAYSTONE_BRIDGE', raising=False)
    packaged = tmp_path / 'bundle' / 'bridge' / 'wow_auth_bridge.exe'
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b'MZ first')
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'bundle'), raising=False)

    first = bridge_executable()
    assert first.parent == data_dir() / 'bin' and first.read_bytes() == b'MZ first'
    first.write_bytes(b'MZ fir')                # cut short by a crash
    assert bridge_executable() == first and first.read_bytes() == b'MZ first'

    packaged.write_bytes(b'MZ second')
    second = bridge_executable()
    assert second != first and second.read_bytes() == b'MZ second'
    assert list(second.parent.iterdir()) == [second]


@pytest.mark.parametrize('pasted', [
    'http://localhost:0/?ST=EU-0123abcd-314159265&flowTrackingId=\n',
    '  EU-0123abcd-314159265  ',
])
def test_sign_in_ticket_is_taken_from_the_redirect_or_pasted_bare(pasted):
    assert parse_ticket(pasted) == b'EU-0123abcd-314159265'


@pytest.mark.parametrize('pasted', [
    '', 'http://localhost:0/?flowTrackingId=', 'http://localhost:0/?ST=', 'EU-0123 abcd', 'x' * 2049,
])
def test_malformed_sign_in_ticket_is_rejected(pasted):
    with pytest.raises(ValueError, match=r'including \?ST='):
        parse_ticket(pasted)


def test_only_the_sign_in_redirect_counts_as_a_clipboard_match():
    assert looks_like_redirect('http://localhost:0/?ST=EU-1-2&flowTrackingId=')
    assert not looks_like_redirect('https://eu.account.battle.net/login/?ST=EU-1-2')


class FakeRuntime:
    bridge = None

    def prepare(self):
        pass


@pytest.mark.parametrize('saved, expected', [
    (True, ['renew', 'sign_in', 'renew']),
    (False, ['sign_in', 'renew']),
])
def test_launch_signs_in_when_the_saved_session_is_rejected_or_missing(tmp_path, monkeypatch, save_login,
                                                                      saved, expected):
    vault = Vault(tmp_path / 'session', bridge=None)
    if saved:
        save_login(vault, 7)
    calls = []

    def renew(*_):
        calls.append('renew')
        if calls.count('renew') == 1 and saved:
            raise CredentialRejected('BGS logon completed with status 3')

    def login(vault, *_):
        calls.append('sign_in')
        return save_login(vault, 7)

    monkeypatch.setattr(launch, 'renew', renew)
    monkeypatch.setattr(launch, 'login', login)
    Launcher(Settings(), library=None, runtime=FakeRuntime(), vault=vault).renew(70658, sign_in=None)
    assert calls == expected
    assert vault.last('renewed') is not None
    events = [line for line in vault.history.read_text().splitlines()]
    assert ('rejected' in events[0]) is saved


def test_other_failures_do_not_trigger_a_sign_in(tmp_path, monkeypatch, save_login):
    vault = Vault(tmp_path / 'session', bridge=None)
    save_login(vault, 7)

    def renew(*_):
        raise RuntimeError('Server closed the BGS connection')

    monkeypatch.setattr(launch, 'renew', renew)
    monkeypatch.setattr(launch, 'login', lambda *_: pytest.fail('signed in after a network failure'))
    with pytest.raises(RuntimeError, match='closed the BGS connection'):
        Launcher(Settings(), library=None, runtime=FakeRuntime(), vault=vault).renew(70658, sign_in=None)


def test_nothing_touches_battle_net_desktop_login():
    # Using Battle.net's own saved login consumes it and signs the app out.
    sources = [*ROOT.glob('src/**/*.py'), *ROOT.glob('bridge/*.[ch]')]
    assert sources
    assert not [path.name for path in sources if 'UnifiedAuth' in path.read_text(encoding='utf-8')]
