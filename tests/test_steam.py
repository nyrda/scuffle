"""Steam's shortcuts.vdf: Scuffle's entries come and go, and nothing else in the file changes."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scuffle import shortcuts, steam

FIXTURE = Path(__file__).parent / 'fixtures/shortcuts.vdf'
# Windows finds Steam in the registry and lists processes differently.
linux_steam = pytest.mark.skipif(sys.platform == 'win32', reason='Linux keeps Steam under the home folder')
COMMAND = ['/home/me/.nix-profile/bin/scuffle', 'play', 'wow', '--shortcut', '--wait']


def png(color: bytes = b'\0') -> bytes:
    """A tiny valid PNG, enough for the marker to go in."""
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    header = struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(b'\0' + color)) + \
        chunk(b'IEND', b'')


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Steam where Linux keeps it, under the test's own home, with two accounts."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_DATA_HOME')
    root = tmp_path / '.local/share/Steam'
    users = [root / 'userdata' / user / 'config' for user in ['111', '222']]
    for config in users:
        config.mkdir(parents=True)
    shutil.copy(FIXTURE, users[0] / 'shortcuts.vdf')
    (root / 'userdata/0').mkdir()           # Steam's anonymous folder holds no account
    (tmp_path / '.steam').mkdir()
    (tmp_path / '.steam/root').symlink_to(root)
    return root, users


def images() -> dict:
    return {'portrait': png(b'\1'), 'wide': png(b'\2'), 'icon': png(b'\3')}


def test_the_fixture_reads_and_writes_back_byte_for_byte():
    data = FIXTURE.read_bytes()
    assert steam.serialize(steam.parse(data)) == data


@linux_steam
def test_adding_and_removing_a_client_keeps_every_other_entry_byte_for_byte(installation):
    root, (first, second) = installation
    original = FIXTURE.read_bytes()
    found = steam.find()
    assert found is not None and found.root == root.resolve() and found.users() == [first, second]
    assert not found.has('wow')

    found.add('wow', 'World of Warcraft', COMMAND, images())

    written = (first / 'shortcuts.vdf').read_bytes()
    # The new entry goes in at the end of the list. Everything before it is the old file.
    assert written.startswith(original[:-2]) and written.endswith(b'\x08\x08\x08')
    assert (first / steam.BACKUP).read_bytes() == original
    # The account without a file gets one with only Scuffle's entry.
    entries = steam.shortcut_list(steam.parse((second / 'shortcuts.vdf').read_bytes()))
    assert [key for _, key, _ in entries] == [b'0']
    entry = entries[0][2]
    assert steam.field(entry, 'AppName') == b'World of Warcraft'
    assert steam.field(entry, 'Exe') == b'"/home/me/.nix-profile/bin/scuffle"'
    assert steam.field(entry, 'LaunchOptions') == b'play wow --shortcut --wait'
    appid = steam.entry_app_id(entry)
    icon = second / 'grid' / f'{appid}_icon.png'
    assert steam.field(entry, 'icon') == str(icon).encode()
    for name in [f'{appid}p.png', f'{appid}.png', f'{appid}_icon.png']:
        assert shortcuts.file_marker(second / 'grid' / name) == 'wow'
    assert found.has('wow')

    # Adding again replaces the entry instead of adding a second one.
    found.add('wow', 'World of Warcraft', COMMAND, images())
    assert (first / 'shortcuts.vdf').read_bytes() == written

    assert found.remove('wow')
    assert (first / 'shortcuts.vdf').read_bytes() == original
    assert not list((second / 'grid').iterdir())
    assert not found.has('wow') and not found.remove('wow')


@linux_steam
def test_only_scuffles_marked_entries_and_artwork_are_removed(installation):
    root, (first, second) = installation
    found = steam.find()
    found.add('wow', 'World of Warcraft', COMMAND, images())
    found.add('wow_classic', 'Mists of Pandaria Classic',
              ['/home/me/.nix-profile/bin/scuffle', 'play', 'wow_classic', '--shortcut', '--wait'], images())
    appid = steam.entry_app_id(steam.shortcut_list(steam.parse((second / 'shortcuts.vdf').read_bytes()))[0][2])
    portrait = second / 'grid' / f'{appid}p.png'
    portrait.write_bytes(png(b'\x09'))       # artwork the user picked in Steam, without a marker
    found.remove('wow')
    names = [steam.field(entry, 'AppName') or steam.field(entry, 'appname')
             for _, _, entry in steam.shortcut_list(steam.parse((first / 'shortcuts.vdf').read_bytes()))]
    # The user's own World of Warcraft entry runs Scuffle too, but was not made by it.
    assert names == ['Heroic – Ünïcode'.encode(), b'Old Emulator', b'World of Warcraft', b'Mists of Pandaria Classic']
    assert portrait.read_bytes() == png(b'\x09')
    assert found.has('wow_classic') and not found.has('wow')


@linux_steam
def test_nothing_is_written_while_steam_runs(installation, tmp_path):
    root, (first, second) = installation
    before = (first / 'shortcuts.vdf').read_bytes()
    # A process that looks like this installation's client, by its path inside the Steam folder.
    # bash keeps the name it was started under. The second command stops it from becoming sleep.
    client = subprocess.Popen([str(tmp_path / '.steam/root/steamrt64/steam'), '-c', 'sleep 30; true'],
                              executable=shutil.which('bash'))
    try:
        found = steam.find()
        assert found.running()
        with pytest.raises(steam.SteamRunning):
            found.add('wow', 'World of Warcraft', COMMAND, images())
    finally:
        client.kill()
        client.wait()
    assert (first / 'shortcuts.vdf').read_bytes() == before
    assert not (second / 'shortcuts.vdf').exists() and not (first / steam.BACKUP).exists()
    assert not steam.find().running()


@linux_steam
@pytest.mark.parametrize('damage', [b'\x09odd\x00', b'', None], ids=['unknown-type', 'cut-short', 'no-access'])
def test_a_file_scuffle_cannot_read_stops_every_change(installation, damage):
    root, (first, second) = installation
    data = FIXTURE.read_bytes()
    # A field of a type with unknown length inside the first entry, or the end missing.
    broken = data[:40] + damage + data[40:] if damage else data[:-5]
    (second / 'shortcuts.vdf').write_bytes(broken)
    found = steam.find()
    if damage is None:
        second.parent.chmod(0)          # an account folder Scuffle may not enter
    try:
        with pytest.raises(PermissionError if damage is None else steam.VdfError):
            found.add('wow', 'World of Warcraft', COMMAND, images())
    finally:
        second.parent.chmod(0o755)
    # Neither account's file changed, though the first one could have been read.
    assert (first / 'shortcuts.vdf').read_bytes() == data
    assert (second / 'shortcuts.vdf').read_bytes() == broken
    assert not (first / 'grid').exists()


@linux_steam
def test_no_steam_without_an_account(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.local/share/Steam/userdata/0').mkdir(parents=True)
    assert steam.find() is None
