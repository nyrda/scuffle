"""Choosing the WoW account the game starts with, from the Battle.net login's game accounts."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from waystone import launch
from waystone.auth import bgs
from waystone.auth.protobuf import blob, fields, integer
from waystone.auth.vault import Vault
from waystone.runtime.game import _launch_arguments
from waystone.settings import Settings

OWNER = 412345678
MAIN = {'id': 2886143, 'name': 'WANDERER', 'region': 'EU'}
SECOND = {'id': 905897642, 'name': 'WoW1', 'region': 'EU'}


def handle(account_id, title=bgs.WOW_TITLE, region=2):
    return integer(1, account_id) + integer(2, title) + integer(3, region)


class Server(bgs.BGS):
    """The AccountService side of a BGS connection, without a socket."""

    def __init__(self, names, fail=None):
        self.names, self.fail, self.asked = names, fail, []

    def call(self, service, method, body):
        assert (service, method) == (bgs.ACCOUNT_SERVICE, bgs.GET_GAME_ACCOUNT_INFO)
        asked = fields(fields(body)[0][2])
        self.asked.append(asked[0][2])
        if self.fail:
            raise self.fail
        return fields(blob(1, integer(1, OWNER) + blob(2, self.names[asked[0][2]])))


def record(*handles):
    return fields(integer(1, OWNER) + b''.join(blob(2, h) for h in handles) + blob(3, 'Player#1234'))


def test_the_login_names_its_wow_accounts_and_skips_other_games():
    server = Server({MAIN['id']: 'WANDERER', SECOND['id']: 'WoW1'})
    found = server.game_accounts(record(handle(MAIN['id']), handle(77, title=0x4f5357), handle(SECOND['id'])))
    assert found == [MAIN, SECOND]
    assert server.asked == [MAIN['id'], SECOND['id']]


@pytest.mark.parametrize('fail, names, region', [
    (RuntimeError('BGS RPC rejected with status 3'), {}, 2),
    (TimeoutError(), {}, 2),
    (None, {MAIN['id']: ''}, 2),
    (None, {MAIN['id']: 'WANDERER'}, 99),
])
def test_names_that_cannot_be_read_are_unknown_rather_than_no_accounts(fail, names, region):
    assert Server(names, fail).game_accounts(record(handle(MAIN['id'], region=region))) is None


def test_a_failed_lookup_keeps_the_list_of_the_same_login_only(tmp_path, save_login):
    vault = Vault(tmp_path / 'session', bridge=None)
    login = save_login(vault)           # an older Waystone's session, whose account id is not known yet
    login.save_accounts(OWNER, [MAIN, SECOND])
    login.save_accounts(OWNER, None)
    assert login.accounts() == {'owner': OWNER, 'game_accounts': [MAIN, SECOND]}
    login.save_accounts(OWNER + 1, None)
    assert login.accounts() is None
    login.save_accounts(OWNER, [MAIN])
    vault.forget()
    assert login.accounts() is None


@pytest.mark.parametrize('saved, listed, expected', [
    ({}, [MAIN], MAIN),                                  # a sole account needs no choice
    ({}, [MAIN, SECOND], None),                          # several and no choice: the game asks
    ({str(OWNER): SECOND['id']}, [MAIN, SECOND], SECOND),
    ({str(OWNER): 1}, [MAIN, SECOND], None),             # the chosen account is gone
    ({str(OWNER): SECOND['id']}, [MAIN], None),          # and not replaced by the one left
    ({str(OWNER): MAIN['id']}, [MAIN], MAIN),
    ({str(OWNER + 1): SECOND['id']}, [MAIN, SECOND], None),   # another login's choice
])
def test_the_game_starts_with_the_saved_or_sole_account(saved, listed, expected):
    settings = Settings(game_accounts=saved)
    assert launch.chosen_account(settings, {'owner': OWNER, 'game_accounts': listed}) == expected


def test_the_bridge_gets_the_account_before_the_game_and_its_arguments():
    command = _launch_arguments(Path('/b/bridge.exe'), Path('/g/Wow.exe'), ['-launcherlogin'],
                                Path('/s/token'), Path('/s/ack'), ('WANDERER', 'EU'))
    game = str(Path('/g/Wow.exe').resolve()) if sys.platform == 'win32' else 'Z:\\g\\Wow.exe'
    assert command[4:] == ['--game-account', 'WANDERER', 'EU', game, '-launcherlogin']
    assert '--game-account' not in _launch_arguments(Path('/b'), Path('/g/Wow.exe'), [], Path('/t'), Path('/a'))


class Library:
    def __init__(self, root):
        self.root = root

    def state(self, code):
        return {'version': '12.1.5.70575'}

    def pending(self, code):
        return None

    def running(self, code):
        return False

    def executable(self, code):
        return self.root / 'Wow.exe'


def test_a_launch_passes_the_account_chosen_for_this_login(tmp_path, monkeypatch, save_login):
    vault = Vault(tmp_path / 'session', bridge=None)
    save_login(vault, OWNER)
    # The renewal lists the accounts again, so the launch reads the list it wrote.
    monkeypatch.setattr(launch, 'renew', lambda login, *_: login.save_accounts(OWNER, [MAIN, SECOND]))
    started = []

    class Runtime:
        bridge = None

        def prepare(self):
            pass

        def start(self, *args, account=None, **kwargs):
            started.append(account)
            return SimpleNamespace(poll=lambda: 1)

    settings = Settings(game_accounts={str(OWNER): SECOND['id']})
    launcher = launch.Launcher(settings, Library(tmp_path), runtime=Runtime(), vault=vault)
    with pytest.raises(launch.LaunchError):
        launcher.play('wow', sign_in=None)
    assert started == [('WoW1', 'EU')]


def test_saved_choices_that_are_not_an_account_id_are_dropped(tmp_path, monkeypatch):
    import json
    from waystone import settings as settings_module
    monkeypatch.setattr(settings_module, 'config_dir', lambda: tmp_path)
    (tmp_path / 'settings.json').write_text(json.dumps({'game_accounts': {
        str(OWNER): SECOND['id'], 'name': 1, '7': 'WoW1', '8': True}}))
    assert Settings.load().game_accounts == {str(OWNER): SECOND['id']}
