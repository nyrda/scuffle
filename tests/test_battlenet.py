"""Check that the client list follows Battle.net's catalog. No test reaches the network.

The catalog files are the ones captured in tests/fixtures/battlenet. The CDN that
serves them is a local HTTP server, read by the real CDNClient.
"""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from scuffle import battlenet, catalog, discovery
from scuffle.ngdp.cdn import CDNClient

FIXTURES = Path(__file__).parent / 'fixtures'
CAPTURED = FIXTURES / 'battlenet'


@pytest.fixture(autouse=True)
def builtin_catalog():
    yield
    catalog.apply(None)


def root():
    return json.loads((CAPTURED / 'root.json').read_text(encoding='utf-8'))


def fragment():
    return json.loads((CAPTURED / 'world_of_warcraft.json').read_text(encoding='utf-8'))


def table(document=None) -> dict:
    return {'format': battlenet.FORMAT, 'checked_at': 1_800_000_000, 'build': 'test',
            'installs': battlenet.parse(root(), document or fragment())}


def products(*extra: str) -> dict:
    """What discovery saved for the US, with more public products, as code=version."""
    found = json.loads((FIXTURES / 'catalog-us.json').read_text(encoding='utf-8'))['products']
    for item in extra:
        code, version = item.split('=')
        found[code] = {'version': version, 'build_config': 'a' * 32, 'public': True, 'locales': ['enUS']}
    return found


# Reading the catalog from the CDN

class CDN:
    """A loopback CDN for the catalogs product. Files are served under their MD5."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        files = self.files

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                data = files.get(self.path.rsplit('/', 1)[-1])
                self.send_response(200 if data is not None else 404)
                self.send_header('Content-Length', str(len(data or b'')))
                self.end_headers()
                self.wfile.write(data or b'')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.versions = []

    def add(self, data: bytes) -> str:
        key = hashlib.md5(data).hexdigest()
        self.files[key] = data
        return key

    def publish(self, name: str, root_document: dict, fragment_document: dict) -> None:
        """A catalog build, as the version service names it, such as PUB-30."""
        document = copy.deepcopy(root_document)
        fragment_key = self.add(json.dumps(fragment_document).encode())
        for entry in document['fragments']:
            if entry['name'] == battlenet.FRAGMENT:
                entry['hash'] = fragment_key
        captured = (CAPTURED / 'build-config').read_text(encoding='utf-8')
        original = next(line.split()[-1] for line in captured.splitlines() if line.startswith('root ='))
        build = self.add(captured.replace(original, self.add(json.dumps(document).encode())).encode())
        self.versions.append(f'{name}|{build}|{"b" * 32}||1|1|')

    def get(self, url: str) -> bytes:
        """The version service, for the catalogs product only."""
        if url == battlenet.VERSIONS.format(region='us'):
            header = (CAPTURED / 'versions.psv').read_text(encoding='utf-8').splitlines()[:2]
            return '\n'.join(header + self.versions).encode()
        if url == battlenet.CDNS.format(region='us'):
            port = self.server.server_address[1]
            return (b'Name!STRING:0|Path!STRING:0|Hosts!STRING:0|Servers!STRING:0|ConfigPath!STRING:0\n'
                    b'us|tpr/catalogs||http://127.0.0.1:%d/?maxhosts=4|tpr/configs/data\n' % port)
        raise OSError(f'not captured: {url}')

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def cdn():
    server = CDN()
    yield server
    server.close()


@pytest.fixture
def loopback(monkeypatch):
    """CDNClient allowing the loopback CDN's plain HTTP, as tests of the transport do."""
    monkeypatch.setattr(battlenet, 'CDNClient', lambda *a, **kw: CDNClient(*a, allow_http=True, backoff=0, **kw))


def test_the_catalog_gives_every_install_uid_name_and_mode(cdn, loopback, tmp_path):
    cdn.publish('PUB-30', root(), fragment())
    installs = battlenet.fetch('us', tmp_path, get=cdn.get)['installs']
    assert installs['wow_anniversary']['uid'] == 'wow_classic_anniversary'
    assert installs['wowlivetest']['uid'] == 'wow_dark' and installs['wowt']['uid'] == 'wow_ptr'
    assert installs['wowf'] == {'uid': 'wowf', 'name': 'WoW: Forever', 'kind': 'live', 'product': 'WoWF',
                                'deprecated': False, 'modes': []}
    assert installs['wow_classic_era']['modes'][2] == ['wow_classic_era_sod', 'Season of Discovery',
                                                       ['-initialgamemode=csod']]
    # Deprecated live types and development builds are not offered as live games.
    assert installs['wow_classic_beta']['kind'] == 'test' and installs['wowe1']['kind'] == 'other'


def test_a_newer_catalog_format_scuffle_cannot_read_falls_back_to_the_older_one(cdn, loopback, tmp_path):
    cdn.publish('PUB-30', root(), fragment())
    changed = root()
    for entry in changed['fragments']:
        entry['name'] = entry['name'].replace(battlenet.FRAGMENT, 'wow')     # a layout Scuffle does not know
    cdn.publish('PUB-31', changed, fragment())
    result = battlenet.fetch('us', tmp_path, get=cdn.get)
    assert result['build'].startswith('PUB-30') and 'wow' in result['installs']
    cdn.publish('PUB-32', root(), fragment())
    assert battlenet.fetch('us', tmp_path, get=cdn.get)['build'].startswith('PUB-32')


def test_a_broken_or_unreachable_catalog_keeps_the_last_good_one(cdn, loopback, tmp_path):
    cdn.publish('PUB-30', root(), fragment())
    good = battlenet.update('us', tmp_path, get=cdn.get)
    assert battlenet.load(tmp_path) == good
    stale = good | {'checked_at': good['checked_at'] - battlenet.TTL - 1}
    battlenet.save(tmp_path, stale)
    # Blizzard publishes a catalog without World of Warcraft in it.
    empty = fragment()
    empty['installs'] = {}
    cdn.versions.clear()
    cdn.publish('PUB-30', root(), empty)
    assert battlenet.update('us', tmp_path, get=cdn.get) == stale
    assert battlenet.load(tmp_path) == stale
    # Or one whose products and types cannot be read, which would leave no game live.
    untyped = fragment()
    del untyped['products']
    cdn.versions.clear()
    cdn.publish('PUB-30', root(), untyped)
    assert battlenet.update('us', tmp_path, get=cdn.get) == stale
    assert battlenet.load(tmp_path) == stale

    def offline(url):
        raise OSError('offline')
    assert battlenet.update('us', tmp_path, get=offline) == stale


def test_a_damaged_cache_is_not_used_and_the_newer_of_cache_and_snapshot_wins(tmp_path):
    from scuffle.catalog_snapshot import SNAPSHOT
    assert battlenet.current(tmp_path)['build'] == SNAPSHOT['build']
    battlenet.path(tmp_path).parent.mkdir(parents=True)
    battlenet.path(tmp_path).write_text('{"format": 1, "installs": {"wow": ')
    assert battlenet.load(tmp_path) is None and battlenet.current(tmp_path)['build'] == SNAPSHOT['build']
    newer = table() | {'checked_at': SNAPSHOT['checked_at'] + 1}
    battlenet.save(tmp_path, newer)
    assert battlenet.current(tmp_path) == newer
    battlenet.save(tmp_path, newer | {'checked_at': SNAPSHOT['checked_at'] - 1})
    assert battlenet.current(tmp_path)['build'] == SNAPSHOT['build']


def test_an_installed_client_the_catalog_stops_listing_keeps_its_install_uid(cdn, loopback, tmp_path):
    from scuffle.catalog_snapshot import SNAPSHOT
    added = fragment()
    added['installs']['wow_forever_live'] = added['installs']['wowf'] | {'tact_product': 'wowf9'}
    cdn.publish('PUB-30', root(), added)
    first = battlenet.update('us', tmp_path, get=cdn.get)
    assert first['installs']['wowf9']['uid'] == 'wow_forever_live'
    battlenet.save(tmp_path, first | {'checked_at': first['checked_at'] - battlenet.TTL - 1})
    cdn.versions.clear()
    cdn.publish('PUB-30', root(), fragment())        # Blizzard removes it again
    assert 'wowf9' not in battlenet.update('us', tmp_path, get=cdn.get)['installs']
    for age in [0, SNAPSHOT['checked_at'] - battlenet.load(tmp_path)['checked_at'] - 1]:
        # From the cache, and once a newer Scuffle's snapshot is used instead of it.
        cached = battlenet.load(tmp_path)
        battlenet.save(tmp_path, cached | {'checked_at': cached['checked_at'] + age})
        discovery.restore(tmp_path, 'us')
        assert catalog.get('wowf9').uid == 'wow_forever_live'
        assert 'wowf9' in [c.code for c in catalog.visible(installed=['wowf9'])]


def test_a_check_of_the_version_service_reads_the_catalog_again_once_it_is_old(cdn, loopback, tmp_path):
    cdn.publish('PUB-30', root(), fragment())
    asked = []

    def get(url):
        asked.append(url)
        if url == discovery.SUMMARY:
            return (FIXTURES / 'version-summary.psv').read_bytes()
        return cdn.get(url)
    discovery.refresh('us', tmp_path, get=get, check=lambda *a: {'public': False})
    assert battlenet.load(tmp_path)['installs']['wowf']['uid'] == 'wowf'
    asked.clear()
    discovery.refresh('us', tmp_path, get=get, check=lambda *a: {'public': False})
    assert battlenet.VERSIONS.format(region='us') not in asked       # it is still fresh


# The client list

def test_a_first_start_without_a_network_lists_the_clients_with_blizzards_install_uids(tmp_path):
    assert discovery.restore(tmp_path, 'us') is None
    uids = {row['tact_product']: uid for uid, row in fragment()['installs'].items()}
    shown = catalog.clients()
    assert [c.code for c in shown if c.category == catalog.LIVE] == [
        'wow', 'wow_classic', 'wow_anniversary', 'wow_classic_era', 'wowf']
    assert {c.code for c in shown if c.test} == {'wowt', 'wowxptr', 'wow_beta', 'wow_classic_ptr',
                                                 'wow_classic_era_ptr', 'wow_classic_beta'}
    for client in shown:
        assert client.uid == uids[client.code], client.code
    era = catalog.get('wow_classic_era')
    assert era.launch_arguments('sod') == ['-launcherlogin', '-uid', 'wow_classic_era', '-initialgamemode=csod']
    # The catalog names no modes for the Era PTR, so Scuffle's own stand in.
    assert [m.id for m in catalog.get('wow_classic_era_ptr').modes] == ['era', 'hardcore', 'sod']


def test_a_product_battle_net_offers_as_live_is_a_live_client_once_it_is_public():
    catalog.apply(products('wow_classic_titan=3.80.0.70000'), table()['installs'])
    titan = catalog.get('wow_classic_titan')
    assert (titan.label, titan.uid, titan.family, titan.category) == (
        'Titan Reforged Classic', 'wow_classic_titan', 'classic', catalog.LIVE)
    assert 'wow_classic_titan' in [c.code for c in catalog.visible()]


def test_an_other_product_takes_its_name_and_install_uid_from_the_catalog():
    catalog.apply(products('wowe2=11.0.0.50000'), table()['installs'])
    event = catalog.get('wowe2')
    assert (event.label, event.uid, event.category, event.parent) == ('WoW Event 2', 'wow_event2', catalog.OTHER, 'wow')
    assert event.launch_arguments() == ['-launcherlogin', '-uid', 'wow_event2']


def with_forever(*, uid: str, code: str, old: str = 'dropped') -> dict:
    """Today's catalog with Forever released under this install UID and product code.

    For a new code, old says what happens to today's wowf: kept as it is, deprecated,
    or dropped from the catalog.
    """
    document = fragment()
    installs, types = document['installs'], document['products'][2]['base']['types']
    release = copy.deepcopy(installs['wowf']) | {'tact_product': code}
    kind = copy.deepcopy(types['wowf']) | {'uid': uid}
    definition = next(d for d in document['types']['definitions'] if d['id'] == 'wowf')
    if code == 'wowf' or old == 'dropped':
        del installs['wowf'], types['wowf']
    elif old == 'deprecated':
        installs['wowf']['deprecated'] = types['wowf']['deprecated'] = True
    installs[uid], types[uid] = release, kind
    document['types']['definitions'].append(definition | {'id': uid})
    return table(document)['installs']


@pytest.mark.parametrize('uid', ['wowf', 'wow_forever'])
def test_forever_released_under_its_code_launches_with_the_catalogs_install_uid(uid):
    catalog.apply(products(), with_forever(uid=uid, code='wowf'))
    forever = catalog.get('wowf')
    assert (forever.label, forever.category, forever.uid) == ('Forever', catalog.LIVE, uid)
    assert 'wowf' in [c.code for c in catalog.visible()]


@pytest.mark.parametrize('uid, old, published', [
    ('wow_forever', 'keep', True),          # the stress test stays in the catalog as a live game
    ('wow_forever', 'deprecated', True),
    ('wow_forever', 'dropped', True),       # gone from the catalog, still on the version service
    ('wow_forever', 'dropped', False),
    ('wowf', 'dropped', True),              # the release takes over the stress test's install UID
    ('wowf', 'dropped', False),
])
def test_forever_released_under_a_new_code_is_listed_as_the_live_game(uid, old, published):
    found = products('wow_forever=1.61.0.71000')
    if not published:
        del found['wowf']
    catalog.apply(found, with_forever(uid=uid, code='wow_forever', old=old))
    release = catalog.get('wow_forever')
    assert (release.family, release.category, release.uid) == ('forever', catalog.LIVE, uid)
    shown = catalog.visible()
    assert 'wow_forever' in [c.code for c in shown]
    stress = catalog.get('wowf')
    if old == 'keep':
        # Nothing tells the two apart, so both stay live and the new one is named by its code.
        assert (stress.category, stress.label) == (catalog.LIVE, 'Forever')
        assert release.label == 'Forever (wow_forever)'
        assert catalog.get('wow_classic_beta').parent == 'wowf'
    else:
        assert release.label == 'Forever' and stress.category == catalog.OTHER
        assert 'wowf' not in [c.code for c in shown]
        # Installed, it stays listed and still launches with its own install UID.
        installed = catalog.visible(installed=['wowf'])
        assert 'wowf' in [c.code for c in installed] and stress.uid == 'wowf'
        assert len({c.label for c in installed}) == len(installed)
        # The Forever beta belongs with the game Battle.net offers now.
        assert catalog.get('wow_classic_beta').parent == 'wow_forever'
    assert len({c.label for c in shown}) == len(shown)
