"""App launcher entries: what they run, and that Waystone removes only its own files."""
import os
from pathlib import Path
import struct
import sys

import pytest

from waystone import catalog, shortcuts

from test_steam import png

linux = pytest.mark.skipif(sys.platform == 'win32', reason='desktop entries are for Linux')
ICONS = {size: png() for size in shortcuts.ICON_SIZES}


@pytest.fixture
def program(tmp_path, monkeypatch):
    """Waystone as a profile installs it: a store path, and a profile link to it on PATH."""
    store = tmp_path / 'store'
    real = store / 'abc-waystone-0.2.0' / 'bin' / 'waystone'
    real.parent.mkdir(parents=True)
    real.write_text('#!/bin/sh\n')
    real.chmod(0o755)
    profile = tmp_path / 'My Profile' / 'bin'
    profile.mkdir(parents=True)
    (profile / 'waystone').symlink_to(real)
    monkeypatch.setenv('NIX_STORE_DIR', str(store))
    # The wrapper puts its own store folder first on PATH.
    monkeypatch.setenv('PATH', os.pathsep.join([str(real.parent), str(profile), os.environ['PATH']]))
    monkeypatch.setattr(sys, 'argv', [str(real), 'play', 'wow', '--shortcut'])
    monkeypatch.delenv('APPIMAGE', raising=False)
    return profile / 'waystone'


@linux
def test_a_shortcut_runs_the_profiles_waystone_rather_than_the_store_path(program, monkeypatch):
    assert shortcuts.program() == [str(program)]
    # Under an AppImage the image's own file is the stable path.
    monkeypatch.setenv('APPIMAGE', '/home/me/Apps/Waystone.AppImage')
    assert shortcuts.shortcut_command('wow', wait=True) == [
        '/home/me/Apps/Waystone.AppImage', 'play', 'wow', '--shortcut', '--wait']


@linux
def test_the_desktop_entry_starts_the_client_and_offers_each_game_mode(program):
    client = catalog.get('wow_classic_era')
    shortcuts.add_to_launcher(client, ICONS)
    entry = shortcuts.desktop_path('wow_classic_era')
    assert entry.parent == Path(os.environ['XDG_DATA_HOME']) / 'applications'
    text = entry.read_text()
    # The space in the profile's folder needs the specification's quotes.
    command = f'"{program}" play wow_classic_era --shortcut'
    assert f'\nExec={command}\n' in text
    assert 'Name=Classic Era\n' in text and 'Icon=waystone-wow_classic_era\n' in text
    assert 'Actions=era;hardcore;sod;\n' in text
    assert f'[Desktop Action hardcore]\nName=Hardcore\nExec={command} --mode hardcore\n' in text
    theme = Path(os.environ['XDG_DATA_HOME']) / 'icons/hicolor'
    for size in shortcuts.ICON_SIZES:
        assert shortcuts.file_marker(theme / f'{size}x{size}/apps/waystone-wow_classic_era.png') == 'wow_classic_era'
    assert shortcuts.in_launcher('wow_classic_era') and not shortcuts.in_launcher('wow')

    assert shortcuts.remove_from_launcher('wow_classic_era')
    assert not entry.exists() and not list(theme.glob('*/apps/*'))
    assert not shortcuts.in_launcher('wow_classic_era')


def test_exec_arguments_are_quoted_and_escaped_as_the_specification_asks():
    assert shortcuts.exec_line(['/opt/Way stone/waystone', '50%', 'a"b', 'c$d', 'C:\\x']) == \
        r'"/opt/Way stone/waystone" 50%% "a\\"b" "c\\$d" "C:\\\\x"'


@linux
def test_files_waystone_did_not_write_are_never_replaced_or_removed(program):
    entry = shortcuts.desktop_path('wow')
    entry.parent.mkdir(parents=True)
    entry.write_text('[Desktop Entry]\nName=Somebody else\'s\n')
    icon = Path(os.environ['XDG_DATA_HOME']) / 'icons/hicolor/256x256/apps/waystone-wow.png'
    icon.parent.mkdir(parents=True)
    icon.write_bytes(png())
    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.add_to_launcher(catalog.get('wow'), ICONS)
    assert not shortcuts.in_launcher('wow') and not shortcuts.remove_from_launcher('wow')
    assert entry.read_text() == '[Desktop Entry]\nName=Somebody else\'s\n' and icon.read_bytes() == png()
    # One for another client is not this client's either.
    entry.write_text(f'[Desktop Entry]\n{shortcuts.MARKER}=wowt\n')
    assert not shortcuts.remove_from_launcher('wow') and entry.exists()


def shell_link(arguments: str, *, unicode: bool = True) -> bytes:
    """A link laid out as MS-SHLLINK describes, with an ID list and link info to skip."""
    flags = 0x1 | 0x2 | 0x4 | 0x20 | (0x80 if unicode else 0)
    header = struct.pack('<I16sI', 0x4C, bytes(16), flags) + bytes(76 - 24)
    id_list = struct.pack('<H', 4) + b'\x02\x00\x00\x00'
    link_info = struct.pack('<I', 8) + bytes(4)

    def string(text):
        return struct.pack('<H', len(text)) + text.encode('utf-16-le' if unicode else 'latin-1')
    return header + id_list + link_info + string('Start World of Warcraft with Waystone') + string(arguments)


def test_a_start_menu_link_is_waystones_by_its_shortcut_argument(tmp_path):
    ours, theirs = tmp_path / 'ours.lnk', tmp_path / 'theirs.lnk'
    ours.write_bytes(shell_link('play wow --shortcut'))
    theirs.write_bytes(shell_link('play wow --wait', unicode=False))
    assert shortcuts.link_strings(ours.read_bytes())['name'] == 'Start World of Warcraft with Waystone'
    assert shortcuts.link_marker(ours, 'wow') and not shortcuts.link_marker(ours, 'wowt')
    assert not shortcuts.link_marker(theirs, 'wow')
    ours.write_bytes(shell_link('play wow --shortcut')[:-6])
    assert not shortcuts.link_marker(ours, 'wow')
