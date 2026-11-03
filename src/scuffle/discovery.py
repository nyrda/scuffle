"""Find the World of Warcraft products Blizzard publishes, and which of them are public.

The version service's summary names every product. Each product's versions table
names its build for a region. A product that is not a live or test client Scuffle
knows counts as public when its product configuration, build configuration and
install manifest download and decode without a decryption key, as an install would
read them. Battle.net's catalog is read again on the same check, see battlenet.py.
The results are cached, so the window starts with the last list and refreshes it in
the background.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Callable
import urllib.request

from . import battlenet, catalog
from .ngdp.cdn import CDNClient, HTTPStatusError, parse_psv
from .ngdp.formats import decode_blte, read_config, read_install

SUMMARY = 'https://us.version.battle.net/v2/summary'
VERSIONS = 'https://{region}.version.battle.net/v2/products/{product}/versions'
TTL = 6 * 3600
FORMAT = 1
KEY = re.compile(r'[0-9a-f]{32}')
GAME = re.compile(r'wow[a-z]*\.exe', re.IGNORECASE)     # Wow.exe, WowT.exe, WowB.exe, WowClassicT.exe


def fetch(url: str, limit: int = 1 << 20, timeout: float = 15) -> bytes:
    """An anonymous HTTPS request with a bounded answer."""
    if not url.startswith('https://'):
        raise ValueError('Only HTTPS is used for the version service')
    request = urllib.request.Request(url, headers={'User-Agent': 'scuffle/0.1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310, HTTPS only
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('The version service answer is too large')
    return data


def parse_summary(data: bytes) -> list[str]:
    """World of Warcraft products with a versions table. Other rows describe CDNs or background downloads."""
    return sorted({row['Product'] for row in parse_psv(data)
                   if row.get('Flags', '') == '' and catalog.is_product(row.get('Product'))})


def parse_versions(data: bytes, region: str) -> dict | None:
    """The build a product publishes for one region, or None when it has none there."""
    for row in parse_psv(data):
        if row.get('Region') != region:
            continue
        if not all(KEY.fullmatch(row.get(name, '')) for name in ('BuildConfig', 'CDNConfig', 'ProductConfig')):
            return None
        return {'version': row.get('VersionsName', ''), 'build_config': row['BuildConfig']}
    return None


def passing(error: Exception) -> bool:
    """A failure that says nothing about the product, such as a timeout. The next refresh asks again."""
    if isinstance(error, HTTPStatusError):
        return error.status == 429 or error.status >= 500
    return isinstance(error, (OSError, http.client.HTTPException))


def probe(code: str, region: str, cache: Path, *, factory: Callable = CDNClient) -> dict:
    """Read a product's metadata as an install would. Anything that needs a key fails.

    A network failure gives no verdict, only 'retry', so the product is checked again.
    """
    from .library import managed_path
    try:
        with factory(cache, region, code, retries=1, timeout=20) as client:
            version, _ = client.discover()
            product = json.loads(client.get_product_config(version['ProductConfig']).read_bytes())
            config = product.get('all', {}).get('config', {})
            if config.get('update_method') != 'ngdp' or config.get('data_dir', 'Data/') != 'Data/':
                raise ValueError('not an NGDP client')
            folder = config.get('shared_container_default_subfolder', '')
            if not folder or '/' in folder or managed_path(folder) != folder:
                raise ValueError('no usable folder name')
            locales = [str(locale) for locale in config.get('supported_locales') or []]
            build = read_config(client.get_config(version['BuildConfig']).read_bytes())
            ckey, ekey = (bytes.fromhex(x) for x in build['install'][:2])
            decoded, encoded = map(int, build['install-size'][:2])
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / 'install'
                decode_blte(client.get_object(ekey, encoded), manifest, ekey, ckey, decoded)
                tags = {'Windows', 'x86_64', region.upper(), 'enUS' if 'enUS' in locales else locales[0]}
                if not any(GAME.fullmatch(entry.path) for entry in read_install(manifest, tags)):
                    raise ValueError('no game executable')
    except Exception as error:      # beyond the network, every failure means the same: not offered
        reason = f'{type(error).__name__}: {error}'[:200]
        return {'retry': True, 'reason': reason} if passing(error) else {'public': False, 'reason': reason}
    return {'public': True, 'locales': locales}


def refresh(region: str, cache: Path, previous: dict | None = None, *, get: Callable | None = None,
            check: Callable | None = None, workers: int = 8) -> dict:
    """Ask the version service again, and Battle.net's catalog when it is old.

    Raises when the version service's summary cannot be read. A catalog that cannot be
    read keeps the last good one.
    """
    get, check = get or fetch, check or probe
    codes = parse_summary(get(SUMMARY))
    battlenet.update(region, cache, get=get)
    earlier = (previous or {}).get('products', {})

    def version(code):
        try:
            return parse_versions(get(VERSIONS.format(region=region, product=code)), region)
        except Exception:
            # One product failing to answer keeps what was known about it.
            return dict(earlier[code]) if code in earlier else None

    with ThreadPoolExecutor(workers) as pool:
        rows = {code: row for code, row in zip(codes, pool.map(version, codes)) if row}
        unknown = []
        for code, row in rows.items():
            if catalog.trusted(code):
                continue
            seen = earlier.get(code, {})
            if seen.get('build_config') == row['build_config'] and 'public' in seen and not seen.get('retry'):
                # The same build was checked before.
                row.update({k: seen[k] for k in ('public', 'locales', 'reason') if k in seen})
            else:
                unknown.append(code)
        for code, result in zip(unknown, pool.map(lambda code: check(code, region, cache), unknown)):
            if result.get('retry'):
                # Until it answers, the last verdict stands, so a public product does not drop out.
                seen = earlier.get(code, {})
                result = {k: seen[k] for k in ('public', 'locales') if k in seen} | result
            rows[code].update(result)
    return {'format': FORMAT, 'region': region, 'checked_at': int(time.time()), 'products': rows}


def path(cache: Path, region: str) -> Path:
    return Path(cache) / 'catalog' / f'{region}.json'


def load(cache: Path, region: str) -> dict | None:
    """The last result for a region, or None. A damaged file counts as none."""
    try:
        snapshot = json.loads(path(cache, region).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get('format') != FORMAT or snapshot.get('region') != region \
            or not isinstance(snapshot.get('products'), dict) or not isinstance(snapshot.get('checked_at'), int):
        return None
    return snapshot


def restore(cache: Path, region: str) -> dict | None:
    """Use the last versions found for a region and the last good Battle.net catalog.

    Returns the versions, or None when there are none yet.
    """
    snapshot = load(cache, region)
    table = battlenet.current(cache)
    catalog.apply((snapshot or {}).get('products'), table['installs'], table.get('dropped'))
    return snapshot


def save(cache: Path, snapshot: dict) -> None:
    from .library import atomic_json
    atomic_json(path(cache, snapshot['region']), snapshot)


def fresh(snapshot: dict | None, now: float | None = None) -> bool:
    if not snapshot:
        return False
    age = (time.time() if now is None else now) - snapshot['checked_at']
    return 0 <= age < TTL
