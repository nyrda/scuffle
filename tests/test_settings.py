"""Check the stored settings and which clients the list shows."""
import json

import pytest

from waystone import catalog, settings as settings_module
from waystone.settings import Settings


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, 'config_dir', lambda: tmp_path)
    return tmp_path / 'settings.json'


@pytest.mark.parametrize('switch, expected', [
    (True, sorted(c.code for c in catalog.CLIENTS if c.test)),
    (False, []),
])
def test_the_old_test_client_switch_becomes_a_per_client_choice(config, switch, expected):
    config.write_text(json.dumps({'root': '/games', 'show_test_clients': switch}))
    assert sorted(Settings.load().extra_clients) == expected


def test_settings_reject_an_unknown_client(config):
    config.write_text(json.dumps({'extra_clients': ['wow_not_a_client']}))
    with pytest.raises(ValueError, match='Unknown clients'):
        Settings.load()


def test_the_list_shows_public_clients_chosen_test_clients_and_installed_ones():
    shown = [c.code for c in catalog.visible(extra=['wow_classic_era_ptr'], installed=['wowt'])]
    assert 'wow_classic_era_ptr' in shown and 'wowt' in shown
    assert 'wow_beta' not in shown
    assert all(code in shown for code in [c.code for c in catalog.CLIENTS if not c.test])
