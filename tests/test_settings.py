"""Check the stored settings and which clients the list shows."""
import json

import pytest

from scuffle import catalog, settings as settings_module
from scuffle.settings import Settings


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


def test_settings_from_before_hidden_clients_still_load(config):
    config.write_text(json.dumps({'root': '/games', 'region': 'us', 'extra_clients': ['wowt'], 'selected': 'wowt'}))
    loaded = Settings.load()
    assert loaded.extra_clients == ['wowt'] and loaded.hidden_clients == [] and not loaded.show_other_clients


def test_client_codes_the_catalog_does_not_know_are_kept_and_broken_entries_dropped(config):
    # The catalog follows what Blizzard publishes, so a code unknown today may return.
    config.write_text(json.dumps({'extra_clients': ['wow_future_ptr', 42, '../escape'], 'hidden_clients': 'wow',
                                  'show_other_clients': 'yes'}))
    loaded = Settings.load()
    assert loaded.extra_clients == ['wow_future_ptr'] and loaded.hidden_clients == []
    assert loaded.show_other_clients is False
    loaded.save()
    assert Settings.load().extra_clients == ['wow_future_ptr']


def test_hiding_and_showing_a_client_again():
    settings = Settings(extra_clients=['wowt'])
    settings.unlisted('wowt')
    settings.unlisted('wow')
    assert settings.extra_clients == [] and settings.hidden_clients == ['wow', 'wowt']
    settings.listed('wowt')
    settings.listed('wow')
    # A test client is shown by choice. A live one only needs to stop being hidden.
    assert settings.extra_clients == ['wowt'] and settings.hidden_clients == []


def test_the_list_shows_public_clients_chosen_test_clients_and_installed_ones():
    shown = [c.code for c in catalog.visible(extra=['wow_classic_era_ptr'], installed=['wowt'])]
    assert 'wow_classic_era_ptr' in shown and 'wowt' in shown
    assert 'wow_beta' not in shown
    assert all(code in shown for code in [c.code for c in catalog.CLIENTS if not c.test])


def test_hidden_clients_leave_the_list_unless_they_have_a_download():
    shown = [c.code for c in catalog.visible(hidden=['wow', 'wowt', 'wowf'], installed=['wowt'],
                                             downloading=['wowf'])]
    assert 'wow' not in shown and 'wowt' not in shown       # installed, and still hidden
    assert 'wowf' in shown                                  # its download keeps it


def test_other_clients_appear_only_while_they_are_shown():
    catalog.apply({'wowe1': {'version': '9.0.1.35078', 'public': True, 'locales': ['enUS']}})
    try:
        chosen = {'extra': ['wowe1'], 'installed': []}
        assert 'wowe1' not in [c.code for c in catalog.visible(**chosen)]
        assert 'wowe1' in [c.code for c in catalog.visible(**chosen, show_other=True)]
        assert 'wowe1' in [c.code for c in catalog.visible(downloading=['wowe1'])]
    finally:
        catalog.apply(None)


def test_launch_options_of_a_discovered_client_survive_a_restart(config):
    from scuffle.launch_options import ClientOptions
    saved = Settings()
    saved.client_launch = {'wowlivetest': ClientOptions(switches=['-console'])}
    saved.save()
    # The catalog has not found the client yet when settings load at start.
    loaded = Settings.load()
    assert loaded.client_launch == saved.client_launch and not loaded.problems


def test_an_installed_other_client_stays_listed_while_other_clients_are_off():
    other = catalog.describe('wowlivetest', '10.2.5.52646')
    codes = lambda **kw: [c.code for c in catalog.visible([other], **kw)]
    assert codes() == [] and codes(extra=['wowlivetest']) == []
    assert codes(installed=['wowlivetest']) == ['wowlivetest']
    assert codes(installed=['wowlivetest'], hidden=['wowlivetest']) == []


def test_old_settings_default_to_manual_resume_no_game_pause_and_notifications_without_the_tray(config):
    config.write_text('{"workers": 8, "bandwidth_limit": 7000000}')
    settings = Settings.load()
    assert not settings.resume_downloads and not settings.pause_while_playing
    assert settings.show_notifications and not settings.keep_in_tray and settings.announced_updates == {}
    assert settings.workers == 8 and settings.bandwidth_limit == 7_000_000


@pytest.mark.parametrize('workers, limit, expected', [
    (0, -1, (1, 0)),
    (1000, 20_000_000_000, (64, 10_000_000_000)),
    ('many', 'fast', (16, 0)),
    (None, None, (16, 0)),
    (True, False, (16, 0)),
    ([], {}, (16, 0)),
])
def test_invalid_download_settings_load_safely(config, workers, limit, expected):
    config.write_text(json.dumps({'workers': workers, 'bandwidth_limit': limit,
                                  'pause_while_playing': 'yes', 'resume_downloads': 1}))
    settings = Settings.load()
    assert (settings.workers, settings.bandwidth_limit) == expected
    assert settings.pause_while_playing is False and settings.resume_downloads is False
    settings.save()
    assert Settings.load().workers == expected[0]
