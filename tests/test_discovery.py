"""Check client discovery from captured version service answers. No test reaches the network."""
import json
from pathlib import Path

import pytest

from waystone import catalog, discovery

FIXTURES = Path(__file__).parent / 'fixtures'
INSTALL_EKEY = bytes.fromhex('69ab17d415ecc051c5eafd9c91f97ae1')
INSTALL_CKEY = bytes.fromhex('acd3d5ba96cb39bf5d3ce10e0e206fff')


@pytest.fixture(autouse=True)
def builtin_catalog():
    yield
    catalog.apply(None)


def snapshot():
    return json.loads((FIXTURES / 'catalog-us.json').read_text())


def service(answers: dict[str, bytes]):
    """A version service that knows only these products, and counts the questions."""
    asked = []

    def get(url):
        asked.append(url)
        if url == discovery.SUMMARY:
            return (FIXTURES / 'version-summary.psv').read_bytes()
        product = url.split('/products/')[1].split('/')[0]
        if product not in answers:
            raise OSError('not captured')
        return answers[product]
    return get, asked


def test_the_summary_names_products_with_a_versions_table():
    codes = discovery.parse_summary((FIXTURES / 'version-summary.psv').read_bytes())
    assert {'wow', 'wowt', 'wowe1', 'wowlivetest', 'wowz', 'wow_classic_titan'} <= set(codes)
    # Rows of other games, and products with only a CDN row, have no build to offer.
    assert 'agent' not in codes and 'wow_classic_era_beta' not in codes and 'wowe2' not in codes


def test_a_versions_table_gives_the_regions_build_or_nothing():
    wow = discovery.parse_versions((FIXTURES / 'versions/wow.psv').read_bytes(), 'us')
    assert wow == {'version': '12.1.5.70575', 'build_config': 'd718abcc42604d8efbd35aecfa5bb75b'}
    assert discovery.parse_versions((FIXTURES / 'versions/wow_classic_titan.psv').read_bytes(), 'us') is None


def test_only_products_without_an_entry_are_probed_and_a_known_build_is_not_probed_again(tmp_path):
    answers = {code: (FIXTURES / f'versions/{code}.psv').read_bytes()
               for code in ['wow', 'wowt', 'wowe1', 'wowv2', 'wow_classic_titan']}
    get, _ = service(answers)
    probed = []

    def check(code, region, cache):
        probed.append(code)
        return {'public': code == 'wowe1', 'locales': ['enUS']}

    first = discovery.refresh('us', tmp_path, get=get, check=check)
    assert sorted(probed) == ['wowe1', 'wowv2']
    wowe1 = first['products']['wowe1']
    assert wowe1['version'] == '9.0.1.35078' and wowe1['public'] is True
    assert 'wow_classic_titan' not in first['products']       # it publishes no US build
    probed.clear()
    second = discovery.refresh('us', tmp_path, first, get=get, check=check)
    assert probed == [] and second['products']['wowe1']['public'] is True


def test_a_product_that_stops_answering_keeps_what_was_known(tmp_path):
    get, _ = service({'wow': (FIXTURES / 'versions/wow.psv').read_bytes()})
    previous = snapshot()
    result = discovery.refresh('us', tmp_path, previous, get=get, check=lambda *a: {'public': False})
    assert result['products']['wowe1'] == previous['products']['wowe1']


def test_offline_discovery_raises_so_the_last_list_stays(tmp_path):
    def offline(url):
        raise OSError('offline')
    with pytest.raises(OSError):
        discovery.refresh('us', tmp_path, get=offline)


class FakeCDN:
    """The parts of CDNClient an install reads first, with Forever's real install manifest."""

    def __init__(self, root: Path, *, build_config: bytes | None = None, product: dict | None = None):
        self.root = root
        self.build_config = build_config
        self.product = product or {'all': {'config': {
            'update_method': 'ngdp', 'data_dir': 'Data/', 'shared_container_default_subfolder': '_event1_',
            'supported_locales': ['enUS', 'deDE']}}}

    def __call__(self, cache, region, code, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def discover(self):
        return {'BuildConfig': 'b' * 32, 'CDNConfig': 'c' * 32, 'ProductConfig': 'd' * 32}, {}

    def get_product_config(self, key):
        path = self.root / 'product.json'
        path.write_text(json.dumps(self.product))
        return path

    def get_config(self, key):
        if self.build_config is None:
            # What an encrypted build looks like through CDNClient: its key does not match.
            raise ValueError('Configuration MD5 does not match its key')
        path = self.root / 'build'
        path.write_bytes(self.build_config)
        return path

    def get_object(self, ekey, size):
        assert ekey == INSTALL_EKEY
        return FIXTURES / 'install.blte'


BUILD = (f'# Build Configuration\n\ninstall = {INSTALL_CKEY.hex()} {INSTALL_EKEY.hex()}\n'
         f'install-size = 24086 {(FIXTURES / "install.blte").stat().st_size}\n').encode()


def test_a_product_whose_install_manifest_decodes_without_keys_is_public(tmp_path):
    result = discovery.probe('wowe1', 'us', tmp_path, factory=FakeCDN(tmp_path, build_config=BUILD))
    assert result == {'public': True, 'locales': ['enUS', 'deDE']}


@pytest.mark.parametrize('cdn', [
    lambda root: FakeCDN(root),                                   # the build configuration needs a key
    lambda root: FakeCDN(root, build_config=BUILD.replace(b'24086', b'24087')),   # the manifest does not decode
    lambda root: FakeCDN(root, build_config=BUILD, product={'all': {'config': {
        'update_method': 'ngdp', 'shared_container_default_subfolder': '../outside', 'supported_locales': ['enUS']}}}),
])
def test_a_product_that_cannot_be_read_like_an_install_is_not_offered(tmp_path, cdn):
    assert discovery.probe('wowv2', 'us', tmp_path, factory=cdn(tmp_path))['public'] is False


def test_the_cache_is_used_only_for_its_region_and_while_fresh(tmp_path):
    saved = snapshot()
    discovery.save(tmp_path, saved)
    assert discovery.load(tmp_path, 'us') == saved
    assert discovery.load(tmp_path, 'eu') is None
    discovery.path(tmp_path, 'eu').write_text(json.dumps(saved))      # another region's answer
    assert discovery.load(tmp_path, 'eu') is None
    assert discovery.fresh(saved, now=saved['checked_at'] + 60)
    assert not discovery.fresh(saved, now=saved['checked_at'] + discovery.TTL + 1)
    discovery.path(tmp_path, 'us').write_text('{"format": 1, "region": "us"')
    assert discovery.load(tmp_path, 'us') is None


def test_discovered_public_products_join_the_catalog_with_names():
    catalog.apply(snapshot()['products'])
    codes = [c.code for c in catalog.clients('enUS')]
    assert {'wowlivetest', 'wowe1', 'wowz'} <= set(codes)
    # Products that need a key are left out, as is one only installable in Chinese.
    assert not {'wowdev', 'wowv', 'wowlivetest2', 'wownev1'} & set(codes)
    assert 'wow_cn_beta' not in codes and 'wow_cn_beta' in [c.code for c in catalog.clients()]
    assert catalog.get('wowlivetest').label == 'Dark Realm Test'
    assert catalog.get('wowz').family == 'classic' and catalog.get('wowz').category == catalog.OTHER
    assert catalog.release('wow').version == '12.1.5.70575'


def test_an_unnamed_public_product_gets_a_label_from_its_code_and_version():
    catalog.apply({'wowq': {'version': '3.4.1.12345', 'build_config': 'a' * 32, 'public': True, 'locales': ['enUS']}})
    client = catalog.get('wowq')
    assert client.label == 'Classic 3.4.1 (wowq)' and client.family == 'classic'
    assert client.launch_arguments() == ['-launcherlogin', '-uid', 'wowq']


def test_a_client_known_only_from_an_old_install_still_has_a_name():
    assert catalog.get('wowv7').label == 'wowv7'
    with pytest.raises(ValueError):
        catalog.get('../wow')


def test_test_and_other_builds_behind_their_live_patch_are_stale():
    catalog.apply(snapshot()['products'])
    assert catalog.stale('wow_beta')                 # 12.0.1 while retail is 12.1.5
    assert catalog.stale('wowz')                     # 1.14.4 while Classic Era is 1.15.9
    assert not catalog.stale('wowt')                 # the next patch
    assert not catalog.stale('wowxptr')              # the same patch, an older build
    assert not catalog.stale('wow_classic_era_ptr')  # Era's PTR carries the anniversary build
    assert not catalog.stale('wow')


def test_a_network_failure_is_no_verdict_and_the_product_is_checked_again(tmp_path):
    answers = {code: (FIXTURES / f'versions/{code}.psv').read_bytes() for code in ['wow', 'wowe1']}
    get, _ = service(answers)

    class Unreachable(FakeCDN):
        def discover(self):
            raise TimeoutError('timed out')

    def unreachable(code, region, cache):
        return discovery.probe(code, region, cache, factory=Unreachable(tmp_path))
    first = discovery.refresh('us', tmp_path, get=get, check=unreachable)
    assert first['products']['wowe1'].get('retry') is True and 'public' not in first['products']['wowe1']
    catalog.apply(first['products'])
    assert 'wowe1' not in [c.code for c in catalog.clients()]
    probed = []

    def answers_now(code, region, cache):
        probed.append(code)
        return {'public': True, 'locales': ['enUS']}
    second = discovery.refresh('us', tmp_path, first, get=get, check=answers_now)
    assert probed == ['wowe1'] and second['products']['wowe1'] == {
        'version': '9.0.1.35078', 'build_config': first['products']['wowe1']['build_config'],
        'public': True, 'locales': ['enUS']}


def test_a_public_product_whose_new_build_cannot_be_reached_stays_offered(tmp_path):
    get, _ = service({code: (FIXTURES / f'versions/{code}.psv').read_bytes() for code in ['wow', 'wowe1']})
    previous = snapshot()
    previous['products']['wowe1']['build_config'] = 'f' * 32        # checked for an earlier build
    result = discovery.refresh('us', tmp_path, previous, get=get,
                               check=lambda *a: {'retry': True, 'reason': 'TimeoutError: timed out'})
    wowe1 = result['products']['wowe1']
    assert wowe1['public'] is True and wowe1['retry'] is True
    probed = []
    discovery.refresh('us', tmp_path, result, get=get, check=lambda code, *a: probed.append(code) or {'public': True})
    assert probed == ['wowe1']


def test_a_server_error_is_retried_and_a_missing_object_is_a_verdict():
    from waystone.ngdp.cdn import HTTPStatusError, IntegrityError
    assert discovery.passing(HTTPStatusError(503, 'https://cdn.invalid/x'))
    assert discovery.passing(ConnectionResetError())
    assert not discovery.passing(HTTPStatusError(404, 'https://cdn.invalid/x'))
    assert not discovery.passing(IntegrityError('Configuration MD5 does not match its key'))


def test_every_install_uid_is_the_one_battle_net_gives_its_product():
    installs = json.loads((FIXTURES / 'battlenet-installs.json').read_text())
    products = {product: uid for uid, product in installs.items()}
    for client in catalog.CLIENTS + catalog.OTHERS:
        assert products[client.code] == client.uid, client.code
