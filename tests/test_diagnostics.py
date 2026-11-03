"""The diagnostics report: what it tells, and the private data it never carries."""
import json
from pathlib import Path

import pytest

from waystone import cli, diagnostics, system
from waystone.downloads import FAILED, QUEUED, Job
from waystone.launch_options import LinuxOptions
from waystone.library import Library
from waystone.settings import Settings

TICKET = 'EU-0123456789abcdef0123456789abcdef-314159265'
BLOB = '01000000d08c9ddf0115d1118c7a00c04fc297eb' * 4      # how a DPAPI blob starts, in hex


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A user zebulon with a home of their own, so every folder Waystone uses lies inside it."""
    home = tmp_path / 'zebulon'
    for name, value in {'HOME': home, 'USERPROFILE': home, 'XDG_CONFIG_HOME': home / '.config',
                        'XDG_DATA_HOME': home / '.local/share', 'XDG_CACHE_HOME': home / '.cache',
                        'APPDATA': home / 'AppData/Roaming', 'LOCALAPPDATA': home / 'AppData/Local',
                        'PATH': home / 'bin', 'USER': 'zebulon', 'LOGNAME': 'zebulon', 'USERNAME': 'zebulon'}.items():
        monkeypatch.setenv(name, str(value))
    (home / 'bin').mkdir(parents=True)
    return home


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def installed_library(root: Path, version='11.2.5.64253') -> Library:
    library = Library(root)
    library.create()
    write(library.client_dir('wow') / 'state.json', json.dumps(
        {'version': version, 'region': 'eu', 'locale': 'enUS', 'folder': '_retail_'}))
    return library


def test_the_report_tells_what_a_bug_needs_and_leaves_out_every_private_detail(home):
    data = home / '.local/share/waystone'
    # The session and everything beside it, as a signed-in user has them.
    write(data / 'session/session.dpapi', 'sessionsecretcontent')
    write(data / 'session/game-token.dpapi', 'gametokensecret')
    write(data / 'session/history.jsonl', json.dumps({'event': 'rejected', 'detail': 'historysecretdetail'}))
    write(data / 'session/accounts.json', json.dumps({'owner': 987654321, 'game_accounts': [
        {'id': 123456789, 'name': 'WANDERER', 'region': 'EU'}, {'id': 223456789, 'name': 'Wowalt', 'region': 'EU'}]}))
    # Files of other programs that a report must not quote.
    write(home / '.local/share/applications/waystone.desktop', 'Exec=desktopsecretexec')
    write(home / '.steam/steam/config/loginusers.vdf', '"AccountName" "steamsecretaccount"')
    proton = home / '.steam/root/compatibilitytools.d/GE-Proton10-3'
    write(proton / 'proton', '#!/usr/bin/env python3\n')
    write(proton / 'files/bin/wine', '')
    write(proton / 'version', '1712345678 GE-Proton10-3\n')
    umu = write(home / 'bin/umu-run', '#!/bin/sh\necho "umu-launcher version 9.9 (3.14.8 (main))"\n')
    umu.chmod(0o755)
    write(data / 'logs/steam-battlenet.log', '\n'.join([
        f'trace:process:start {home}/Games/Waystone/_retail_/Wow.exe --game-account WANDERER EU',
        f'"WEB_TOKEN"="{TICKET}"',
        # An account from an earlier sign-in, which the saved list no longer names.
        'trace:reg:RegSetValueExW L"GAME_ACCOUNT" type 1 L"OLDACCOUNT"',
        'wine: process exited normally']))
    settings = Settings(root=str(home / 'Games/Waystone'), proton=str(proton), workers=12,
                        game_accounts={'987654321': 123456789},
                        linux_launch=LinuxOptions(gamemode=True, environment={'DXVK_HUD': 'hunter2value'},
                                                  wrapper='gamescope --mode-secretarg --'))
    library = installed_library(home / 'Games/Waystone')
    error = (f'Download failed at {home}/Games/Waystone for WANDERER (account 123456789, owner 987654321, '
             f'zeb.private@example.com): https://eu.actual.battle.net/login?ST={TICKET}&x=1 blob {BLOB}')
    jobs = [Job('wow_classic_era_ptr', 'install', status=FAILED, error=error),
            Job('wow_classic', 'update', status=QUEUED, done_bytes=1, total_bytes=4)]

    text = diagnostics.report(settings, library, jobs, updates={'wow': False}, qt_platform='wayland')

    for secret in ['zebulon', str(home), 'WANDERER', 'Wowalt', '987654321', '123456789', '223456789', TICKET,
                   'sessionsecretcontent', 'gametokensecret', 'historysecretdetail', BLOB, 'zeb.private@example.com',
                   'hunter2value', 'mode-secretarg', 'OLDACCOUNT', 'desktopsecretexec', 'steamsecretaccount']:
        assert secret.lower() not in text.lower()
    assert 'World of Warcraft (wow): 11.2.5.64253, eu, enUS, up to date' in text
    assert 'Folder: ~/Games/Waystone, a Waystone installation' in text
    assert 'Connections: 12, speed limit none' in text
    assert 'Battle.net: signed in, 2 WoW accounts, one chosen' in text
    assert 'install, failed' in text and 'Error: Download failed at ~/Games/Waystone for <account>' in text
    assert 'update, queued, 25%' in text
    if not system.WINDOWS:
        assert 'Proton: GE-Proton10-3, version GE-Proton10-3, chosen, ~/.steam/root/compatibilitytools.d' in text
        assert 'umu-run: umu-launcher version 9.9, ~/bin/umu-run' in text
        assert 'Launch options: GameMode' in text and 'Wrapper: gamescope' in text
        assert 'Environment: DXVK_HUD, values left out' in text
        assert 'File: ~/.local/share/waystone/logs/steam-battlenet.log' in text
        assert '| wine: process exited normally' in text


def test_a_report_comes_together_when_its_sources_are_missing_or_broken(home):
    root = home / 'Games/Waystone'
    library = installed_library(root)
    (library.client_dir('wow') / 'state.json').write_text('{not json')
    (home / '.local/share/waystone/logs/steam-battlenet.log').mkdir(parents=True)  # a log that is no file
    write(home / '.local/share/waystone/session/accounts.json', '[unreadable')
    settings = Settings(root=str(root), proton=str(home / 'gone/GE-Proton9-1'))

    text = diagnostics.report(settings, library, [])

    assert 'Clients\n  Not available (JSONDecodeError)\n' in text
    assert 'Downloads\n  None\n' in text and 'Battle.net: not signed in' in text
    if not system.WINDOWS:
        assert 'Proton: GE-Proton9-1, chosen, missing at ~/gone/GE-Proton9-1' in text
        assert 'umu-run: not found' in text and 'No log yet.' in text


def test_on_windows_the_report_leaves_out_the_linux_runtime(home, monkeypatch):
    monkeypatch.setattr(system, 'WINDOWS', True)
    text = diagnostics.report(Settings(), None, [])
    assert 'Runtime' not in text and 'Proton' not in text and 'Not available' not in text
    assert 'System: Windows' in text and 'Folder: not chosen' in text


def test_the_command_line_prints_the_saved_queue_even_with_broken_settings(home, capsys):
    root = home / 'Games/Waystone'
    installed_library(root)
    write(home / '.config/waystone/settings.json', json.dumps({'region': 'nowhere'}))
    write(home / '.local/share/waystone/downloads.json', json.dumps({'root': str(root.resolve()), 'jobs': [
        {'client': 'wowt', 'kind': 'install', 'status': 'failed', 'error': 'The CDN did not answer'}]}))

    cli.main(['--root', str(root), 'diagnostics'])

    text = capsys.readouterr().out
    assert text.startswith('Waystone diagnostics\n')
    assert '(wowt): install, failed\n    Error: The CDN did not answer' in text
    assert '(wow): 11.2.5.64253, eu, enUS, not checked' in text
