"""Finding installations to import and copying their settings, with folders made in a temporary home."""
import os
from pathlib import Path

import pytest

from waystone import imports
from waystone.auth import protobuf
from waystone.imports import NotImportable, Place, Search


def battle_net(folder: Path, clients: dict[str, str], *, listed=None, executables=True) -> Path:
    """A folder laid out as Battle.net installs one: .build.info, Data/data and one folder per client."""
    (folder / 'Data/data').mkdir(parents=True)
    (folder / 'Data/data/0000000001.idx').write_bytes(b'')
    products = clients.values() if listed is None else listed
    (folder / '.build.info').write_text('Branch!STRING:0|Product!STRING:0\n' +
                                        ''.join(f'eu|{code}\n' for code in products))
    for name, code in clients.items():
        (folder / name).mkdir()
        if code:
            (folder / name / '.flavor.info').write_text(f'Product Flavor!STRING:0\n{code}\n')
        if executables:
            (folder / name / 'Wow.exe').write_bytes(b'inert')
    return folder


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                                         p.lstat().st_mtime_ns)
            for p in sorted(folder.rglob('*'))} | {'.': folder.stat().st_mtime_ns}


def test_inspect_names_the_clients_by_flavor_and_folder_and_skips_leftovers(tmp_path):
    folder = battle_net(tmp_path / 'World of Warcraft',
                        {'_retail_': 'wow', '_classic_era_': '', '_ptr_': 'wowt', '_beta_': 'wow_beta'},
                        listed=['wow', 'wow_classic_era', 'wowt'], executables=False)
    (folder / '_classic_era_/WowClassic.exe').write_bytes(b'inert')
    found = imports.inspect(folder)
    # _beta_ is a leftover: Battle.net no longer lists it and it has no game.
    assert list(found.clients) == ['wow', 'wow_classic_era', 'wowt']
    assert found.clients['wow_classic_era'] == '_classic_era_'
    # Choosing a client's own folder imports the installation around it.
    assert imports.inspect(folder / '_retail_').path == folder


@pytest.mark.parametrize('case, reason', [
    ('empty', 'No World of Warcraft installation'),
    ('waystone', 'Waystone installation folder'),
    ('no_content', 'no game content'),
    ('unknown', 'no client Waystone knows'),
])
def test_inspect_explains_why_a_folder_cannot_be_imported(tmp_path, case, reason):
    folder = tmp_path / 'chosen'
    folder.mkdir()
    if case == 'waystone':
        battle_net(folder / 'x', {'_retail_': 'wow'})
        folder = folder / 'x'
        (folder / '.waystone').mkdir()
    elif case == 'no_content':
        battle_net(folder / 'x', {'_retail_': 'wow'})
        folder = folder / 'x'
        (folder / 'Data/data/0000000001.idx').unlink()
    elif case == 'unknown':
        battle_net(folder / 'x', {'_mystery_': ''}, listed=[])
        folder = folder / 'x'
    with pytest.raises(NotImportable, match=reason):
        imports.inspect(folder)


def product_db(paths: list[str]) -> bytes:
    """Battle.net's product.db: installs, each with its settings holding the installation path."""
    def install(path):
        return protobuf.blob(1, 'wow_enus') + protobuf.blob(2, 'wow') + protobuf.blob(3, protobuf.blob(1, path))
    return b''.join(protobuf.blob(1, install(path)) for path in paths)


def test_search_finds_prefixes_steam_and_drives_and_leaves_the_waystone_folder_out(tmp_path):
    home = tmp_path / 'home'
    battle_net(home / '.wine/drive_c/Program Files (x86)/World of Warcraft', {'_retail_': 'wow'})
    # A Lutris prefix with the game in a place only Battle.net's own record names.
    lutris = home / 'Games/battlenet'
    battle_net(tmp_path / 'elsewhere/Blizzard/World of Warcraft', {'_classic_': 'wow_classic'})
    (lutris / 'drive_c/ProgramData/Battle.net/Agent').mkdir(parents=True)
    (lutris / 'drive_c/ProgramData/Battle.net/Agent/product.db').write_bytes(
        product_db(['D:/Blizzard/World of Warcraft', 'C:/missing']))
    (lutris / 'dosdevices').mkdir()
    (lutris / 'dosdevices/d:').symlink_to(tmp_path / 'elsewhere')
    battle_net(home / '.local/share/Steam/steamapps/compatdata/2501/pfx/drive_c/Games/World of Warcraft',
               {'_classic_era_': 'wow_classic_era'})
    drive = tmp_path / 'mnt'
    battle_net(drive / 'games/World of Warcraft', {'_anniversary_': 'wow_anniversary'})
    waystone = drive / 'games/Waystone'
    battle_net(waystone, {'_retail_': 'wow'})
    places = [p for p in imports.linux_places(home) if p.path.is_relative_to(home)]
    places.append(Place(drive, 'Mounted drive', 3))
    search = Search(places, exclude=waystone)
    found = {f.path: f for f in search}
    assert search.complete
    assert {path.relative_to(tmp_path).as_posix(): f.where for path, f in found.items()} == {
        'home/.wine/drive_c/Program Files (x86)/World of Warcraft': 'Wine prefix',
        'elsewhere/Blizzard/World of Warcraft': 'Wine prefix battlenet',
        'home/.local/share/Steam/steamapps/compatdata/2501/pfx/drive_c/Games/World of Warcraft':
            'Steam prefix 2501',
        'mnt/games/World of Warcraft': 'Mounted drive',
    }


def test_search_never_follows_links_and_ends_on_a_loop(tmp_path):
    drive = tmp_path / 'drive'
    (drive / 'a').mkdir(parents=True)
    (drive / 'a/loop').symlink_to(drive)
    battle_net(tmp_path / 'outside/World of Warcraft', {'_retail_': 'wow'})
    (drive / 'a/link').symlink_to(tmp_path / 'outside')
    search = Search([Place(drive, 'Mounted drive', 50)], folders=10_000)
    assert list(search) == [] and search.complete


@pytest.mark.parametrize('budget', ['seconds', 'folders'])
def test_search_stops_at_its_budget_and_says_so(tmp_path, budget):
    deep = tmp_path / 'drive'
    for index in range(30):
        (deep / f'folder{index:02}' / 'inner').mkdir(parents=True)
    battle_net(deep / 'zz/World of Warcraft', {'_retail_': 'wow'})
    limits = {'seconds': 0} if budget == 'seconds' else {'folders': 5}
    search = Search([Place(deep, 'Mounted drive', 3)], **limits)
    assert list(search) == [] and not search.complete


def test_copying_settings_keeps_the_targets_files_and_never_writes_the_source(tmp_path):
    source = tmp_path / 'bnet/_retail_'
    files = {'WTF/Config.wtf': b'SET gxApi "D3D12"\n', 'WTF/Account/ME/bindings-cache.wtf': b'bind',
             'WTF/Account/ME/macros-cache.txt': b'mine', 'Interface/AddOns/Bag/Bag.toc': b'## Title: Bag',
             'Logs/huge.log': b'not settings'}
    for name, data in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    (tmp_path / 'private.txt').write_bytes(b'outside the client folder')
    (source / 'Interface/AddOns/linked.txt').symlink_to(tmp_path / 'private.txt')
    (source / 'Interface/AddOns/Dev').symlink_to(tmp_path)
    target = tmp_path / 'waystone/_retail_'
    default = b'SET portal "EU"\n'
    (target / 'WTF/Account/ME').mkdir(parents=True)
    (target / 'WTF/Config.wtf').write_bytes(default)
    (target / 'WTF/Account/ME/macros-cache.txt').write_bytes(b'already here')
    before = snapshot(tmp_path / 'bnet')
    result = imports.copy_settings(source, target, replaceable={'WTF/Config.wtf': default})
    assert snapshot(tmp_path / 'bnet') == before
    assert (target / 'WTF/Config.wtf').read_bytes() == files['WTF/Config.wtf']
    assert (target / 'WTF/Account/ME/bindings-cache.wtf').read_bytes() == b'bind'
    assert (target / 'WTF/Account/ME/macros-cache.txt').read_bytes() == b'already here'
    assert (target / 'Interface/AddOns/Bag/Bag.toc').read_bytes() == b'## Title: Bag'
    assert not (target / 'Logs').exists()
    assert not (target / 'Interface/AddOns/linked.txt').exists() and not (target / 'Interface/AddOns/Dev').exists()
    assert (result['copied'], result['kept'], result['skipped']) == (3, 1, 2)
    # Running it again changes nothing: an edited Config.wtf is the user's now.
    (target / 'WTF/Config.wtf').write_bytes(b'edited')
    assert imports.copy_settings(source, target, replaceable={'WTF/Config.wtf': default})['copied'] == 0
    assert (target / 'WTF/Config.wtf').read_bytes() == b'edited'


def test_copying_settings_never_writes_through_a_link_in_the_target(tmp_path):
    source = tmp_path / 'bnet/_retail_'
    (source / 'WTF').mkdir(parents=True)
    (source / 'WTF/Config.wtf').write_bytes(b'user')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    target = tmp_path / 'waystone/_retail_'
    target.mkdir(parents=True)
    (target / 'wtf').symlink_to(elsewhere, target_is_directory=True)
    result = imports.copy_settings(source, target)
    assert os.listdir(elsewhere) == [] and result['copied'] == 0
