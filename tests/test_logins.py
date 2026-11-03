"""Several Battle.net logins: adding, renewing, switching and signing out, and the move from one session."""
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from waystone import launch
from waystone.auth import bgs, vault as vault_module
from waystone.auth.bgs import CredentialRejected
from waystone.auth.protobuf import blob, fields, integer
from waystone.auth.vault import Vault
from waystone.settings import Settings

MAIN, ALT, THIRD = 412345678, 523456789, 634567890
EMAIL = 'player@example.com'
VERSION = 70575


class Bridge:
    """DPAPI as a reversible wrapper, so a test can tell a protected ticket from a plain one."""

    def protect(self, value):
        return b'dpapi:' + value

    def unprotect(self, protected):
        assert protected.startswith(b'dpapi:'), 'an unprotected ticket was saved'
        return protected[len(b'dpapi:'):]

    def device_id(self):
        return b'{}'


class Battlenet:
    """Battle.net's side of the logon, without a socket. Every ticket works once, as on the server."""

    def __init__(self, tags=None):
        self.tickets = {}           # unused ticket -> the account it signs in to
        self.redirects = {}         # browser redirect ticket -> the account the browser signed in to
        self.verified = []          # every redirect ticket presented
        self.tags = tags or {}
        self.issued = 0

    def ticket(self, owner) -> bytes:
        self.issued += 1
        ticket = f'EU-{self.issued}'.encode()
        self.tickets[ticket] = owner
        return ticket

    def browser(self, owner):
        """A browser sign-in to owner, as the sign-in page returns its redirect ticket."""
        def sign_in(url):
            ticket = f'ST-{owner}-{len(self.redirects)}'.encode()
            self.redirects[ticket] = owner
            return ticket
        return sign_in

    def record(self, owner):
        # The email is in the record too, and must never become a label or be stored.
        tag = self.tags.get(owner)
        return fields(integer(1, owner) + (blob(3, tag) if tag else b'') + blob(4, EMAIL))

    def connection(self, host):
        server = self

        class Connection:
            socket = SimpleNamespace(close=lambda: None)
            owner = None

            def connect(self):
                pass

            def web_challenge(self, device, version):
                return 'https://eu.account.battle.net/login'

            def verify_web_ticket(self, ticket):
                server.verified.append(ticket)
                self.owner = server.redirects.pop(ticket)
                return server.record(self.owner)

            def logon(self, credential, device, version):
                if credential not in server.tickets:
                    raise CredentialRejected('BGS logon completed with status 3')
                self.owner = server.tickets.pop(credential)
                return server.record(self.owner)

            def generate_auth_token(self, title):
                return server.ticket(self.owner)

            def game_accounts(self, record):
                return [{'id': self.owner % 1000, 'name': f'WOW{self.owner % 1000}', 'region': 'EU'}]

        return Connection()


class Runtime:
    bridge = Bridge()

    def __init__(self):
        self.started = []

    def prepare(self):
        pass


@pytest.fixture
def battlenet(monkeypatch):
    server = Battlenet(tags={MAIN: 'Wanderer#7395'})
    monkeypatch.setattr(bgs, 'BGS', server.connection)
    return server


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / 'session', Bridge())


def launcher(vault):
    return launch.Launcher(Settings(), library=None, runtime=Runtime(), vault=vault)


def never(url):
    raise AssertionError('asked to sign in through the browser')


def add(vault, battlenet, owner):
    return launcher(vault).sign_in(VERSION, battlenet.browser(owner))


def saved_ticket(login) -> bytes:
    return Bridge().unprotect(login.session.read_bytes())


def older_session(vault, battlenet, owner, listed=True):
    """The one session an older Waystone keeps directly in its session folder."""
    vault.directory.mkdir(mode=0o700, parents=True)
    vault.legacy_session.write_bytes(Bridge().protect(battlenet.ticket(owner)))
    if listed:
        vault.legacy_accounts.write_text(json.dumps({'owner': owner, 'game_accounts': [
            {'id': 1, 'name': 'WANDERER', 'region': 'EU'}]}))


def owner_only(path) -> bool:
    return sys.platform == 'win32' or stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize('listed', [True, False])
def test_an_older_session_moves_to_its_account_and_renews_without_a_new_sign_in(vault, battlenet, listed):
    older_session(vault, battlenet, MAIN, listed)
    ticket = Bridge().unprotect(vault.legacy_session.read_bytes())

    # Moved, not used: the ticket is the same one, so the user is still signed in.
    login = vault.active()
    assert saved_ticket(login) == ticket and not vault.legacy_session.exists()
    assert not vault.legacy_accounts.exists()
    assert login.label == 'Account 1'
    if listed:
        assert login.id == MAIN and login.accounts()['game_accounts'][0]['name'] == 'WANDERER'

    renewed = launcher(vault).renew(VERSION, never)
    # The renewal learns the account id of a session that had no WoW account list.
    assert [found.key for found in vault.logins()] == [str(MAIN)] and renewed.id == MAIN
    assert vault.active().label == 'Wanderer#7395'
    assert saved_ticket(vault.active()) in battlenet.tickets
    for path in [vault.directory, vault.logins_folder, renewed.directory, *renewed.directory.iterdir()]:
        assert owner_only(path), path


def test_a_move_cut_short_by_a_crash_leaves_the_older_session_usable(vault, battlenet, monkeypatch):
    older_session(vault, battlenet, MAIN)
    ticket = Bridge().unprotect(vault.legacy_session.read_bytes())

    replace = os.replace

    def crash(source, target):
        # At the commit, after the new folder and its files were written.
        if source == vault.legacy_session:
            raise OSError('power lost')
        replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(vault_module.os, 'replace', crash)
        # Still signed in, and listed where it was, while the move cannot finish.
        assert [login.key for login in vault.logins()] == [str(MAIN)] and vault.legacy_session.exists()
        with vault.locked():
            assert vault.active().session == vault.legacy_session
        assert (vault.logins_folder / str(MAIN) / 'accounts.json').exists()

    assert saved_ticket(launcher(vault).renew(VERSION, never)) in battlenet.tickets
    assert ticket not in battlenet.tickets and not vault.legacy_session.exists()


def test_a_move_that_stopped_after_its_commit_only_leaves_the_old_list_to_delete(vault, battlenet):
    older_session(vault, battlenet, MAIN)
    login = vault.active()
    # As a crash between moving the session and deleting the old list leaves it.
    vault.legacy_accounts.write_text(json.dumps({'owner': MAIN, 'game_accounts': []}))
    assert [found.key for found in vault.logins()] == [str(MAIN)]
    launcher(vault).renew(VERSION, never)
    assert not vault.legacy_accounts.exists() and login.accounts()['game_accounts'][0]['name'] == 'WOW678'



def test_an_older_session_that_cannot_take_its_accounts_folder_leaves_the_listed_one_usable(
        vault, battlenet, monkeypatch):
    older_session(vault, battlenet, MAIN, listed=False)
    vault.logins()
    main = add(vault, battlenet, MAIN)          # the same account, added again before the old one renewed
    ticket = saved_ticket(main)
    vault.activate('unknown')
    replace = os.replace

    def fail(source, target):
        if str(source).endswith('unknown'):
            raise OSError('disk full')
        replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(vault_module.os, 'replace', fail)
        with pytest.raises(OSError, match='disk full'):
            launcher(vault).renew(VERSION, never)
    assert saved_ticket(main) == ticket and ticket in battlenet.tickets
    assert str(MAIN) in [login.key for login in vault.logins()]

def test_adding_accounts_keys_each_by_its_account_id_and_makes_it_active(vault, battlenet):
    first = add(vault, battlenet, MAIN)
    second = add(vault, battlenet, ALT)
    assert [login.key for login in vault.logins()] == [str(MAIN), str(ALT)]
    assert vault.active().key == str(ALT)
    # Each redirect ticket went to one VerifyAuthToken, for the account being added.
    assert len(battlenet.verified) == 2 and not battlenet.redirects
    assert (first.label, second.label) == ('Wanderer#7395', 'Account 2')
    # Each login keeps its own WoW accounts, and nothing stored names the email or a plain ticket.
    assert first.accounts()['game_accounts'][0]['name'] == 'WOW678'
    assert second.accounts()['game_accounts'][0]['name'] == 'WOW789'
    for path in vault.directory.rglob('*'):
        if path.is_file():
            data = path.read_bytes()
            assert EMAIL.encode() not in data and b'EU-' not in data.replace(b'dpapi:EU-', b''), path

    # Signing in again to a listed account replaces its session and keeps its place.
    old = saved_ticket(first)
    add(vault, battlenet, MAIN)
    assert [login.key for login in vault.logins()] == [str(MAIN), str(ALT)]
    assert saved_ticket(first) != old and vault.active().key == str(MAIN)
    assert first.label == 'Wanderer#7395' and second.label == 'Account 2'


def test_signing_out_of_one_account_keeps_the_others_and_their_numbers(vault, battlenet):
    first, second, third = (add(vault, battlenet, owner) for owner in (THIRD, ALT, MAIN))
    tickets = {login.key: saved_ticket(login) for login in (first, third)}
    vault.activate(second.key)
    vault.forget()
    assert not second.directory.exists()
    assert [login.key for login in vault.logins()] == [first.key, third.key]
    assert {login.key: saved_ticket(login) for login in vault.logins()} == tickets
    # The next by number is active, and no label moves to another account.
    assert vault.active().key == first.key and first.label == 'Account 1' and third.label == 'Wanderer#7395'
    # A login added later takes the free number.
    assert add(vault, battlenet, ALT).label == 'Account 2'


def test_each_account_renews_as_itself(vault, battlenet):
    add(vault, battlenet, MAIN)
    add(vault, battlenet, ALT)
    vault.activate(str(MAIN))
    renewed = launcher(vault).renew(VERSION, never)
    assert renewed.key == str(MAIN) and battlenet.tickets[saved_ticket(renewed)] == MAIN
    vault.activate(str(ALT))
    renewed = launcher(vault).renew(VERSION, never)
    assert renewed.key == str(ALT) and battlenet.tickets[saved_ticket(renewed)] == ALT


def test_a_session_that_signs_in_to_another_account_is_never_saved_as_it(vault, battlenet):
    main, alt = add(vault, battlenet, MAIN), add(vault, battlenet, ALT)
    alt_ticket = saved_ticket(alt)
    # MAIN's saved ticket signs in to ALT, as a mixed-up file would.
    main.save_session(battlenet.ticket(ALT))
    with pytest.raises(CredentialRejected, match='another Battle.net account'):
        bgs.renew(main, 'eu.actual.battle.net', VERSION)
    assert saved_ticket(alt) == alt_ticket and alt.accounts()['owner'] == ALT
    assert main.accounts()['owner'] == MAIN and not (vault.directory / 'game-token.dpapi').exists()


def test_a_launch_sign_in_to_another_account_adds_it_and_stops(vault, battlenet):
    third = add(vault, battlenet, THIRD)
    third_ticket = saved_ticket(third)
    main = add(vault, battlenet, MAIN)
    battlenet.tickets.pop(saved_ticket(main))       # Battle.net ended MAIN's session
    with pytest.raises(launch.LaunchError, match='Wanderer#7395.*Account 2'):
        launcher(vault).renew(VERSION, battlenet.browser(ALT))
    # Only the ended session is gone. The account signed in to instead is listed and shown as active.
    assert [login.key for login in vault.logins()] == [str(THIRD), str(ALT)]
    assert vault.active().key == str(ALT) and saved_ticket(third) == third_ticket


def test_switching_during_a_launch_waits_for_it_and_never_changes_its_account(vault, battlenet, monkeypatch):
    add(vault, battlenet, ALT)
    add(vault, battlenet, MAIN)
    attempts = []

    class Library:
        def state(self, code):
            return {'version': f'12.1.5.{VERSION}'}

        def moving(self):
            return False

        def pending(self, code):
            return None

        def running(self, code):
            return False

        def executable(self, code):
            return vault.directory / 'Wow.exe'

    class Starting(Runtime):
        def start(self, executable, arguments, token, ack, *, account=None, **kwargs):
            # Another window switches while this launch holds its session.
            try:
                Vault(vault.directory, None).activate(str(ALT))
            except RuntimeError as error:
                attempts.append(str(error))
            self.started.append((account, Bridge().unprotect(token.read_bytes())))
            return SimpleNamespace(poll=lambda: 1)

    runtime = Starting()
    with pytest.raises(launch.LaunchError, match='did not start'):
        launch.Launcher(Settings(), Library(), runtime=runtime, vault=vault).play('wow', never)
    [(account, token)] = runtime.started
    assert attempts and account == ('WOW678', 'EU') and battlenet.tickets[token] == MAIN
    assert vault.active().key == str(MAIN)
    Vault(vault.directory, None).activate(str(ALT))
    assert vault.active().key == str(ALT)


@pytest.mark.parametrize('text, tag', [
    ('Wanderer#7395', 'Wanderer#7395'), ('Ärger#12345', 'Ärger#12345'),
    (EMAIL, None), ('player#7395@example.com', None), ('EU-0123abcd-314159265', None), ('#7395', None),
])
def test_only_a_battle_tag_becomes_a_label(text, tag):
    assert bgs.logon_battle_tag(fields(integer(1, MAIN) + blob(4, text))) == tag
