"""Battle.net's public catalog of World of Warcraft installs.

The catalog is the `catalogs` product on Blizzard's CDN. Its versions table has one
row per catalog format, such as PUB-30, and each row's build configuration names a
root file. The root lists fragments, and the `world_of_warcraft` fragment holds an
`installs` table that maps each install UID to its NGDP product. The WoW product's
`types` give each UID a name, a category such as live or in development, and the
game modes Battle.net offers for it. Every file is plain JSON whose MD5 is its key.

Scuffle reads this into a small table keyed by product code. The last good table is
cached, and a snapshot of it ships with Scuffle for a first start without a network.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Callable

from . import catalog
from .ngdp.cdn import CDNClient, parse_config, parse_psv

VERSIONS = 'https://{region}.version.battle.net/v2/products/catalogs/versions'
CDNS = 'https://{region}.version.battle.net/v2/products/catalogs/cdns'
FRAGMENT = 'world_of_warcraft'
TTL = 6 * 3600
FORMAT = 1
TRIES = 3                       # catalog formats tried, newest first
LIMIT = 8 * 1024 * 1024         # the root and the fragment are each a few hundred KiB
PUBLISHED = re.compile(r'PUB-(\d{1,4})')
UID = re.compile(r'[a-z0-9_]{1,64}')
ARGUMENT = re.compile(r'-[A-Za-z0-9_.]+(=[A-Za-z0-9_.]+)?')     # such as -initialgamemode=cera
LIVE_CATEGORY = 'default#CATEGORIES_LIVE_NAME'
TEST_MARKER = re.compile(r'ptr|beta')
KINDS = (catalog.LIVE, catalog.TEST, catalog.OTHER)


# Reading the catalog

def _definitions(document: dict, name: str) -> list[dict]:
    value = document.get(name)
    rows = value.get('definitions') if isinstance(value, dict) else None
    return [row for row in rows or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]


def _strings(document: dict) -> dict[str, str]:
    strings = document.get('strings')
    table = strings.get('default') if isinstance(strings, dict) else None
    return {k: v for k, v in (table or {}).items() if isinstance(k, str) and isinstance(v, str)}


def _types(fragment: dict) -> dict[str, tuple[str, dict, dict]]:
    """Each install UID's type id, type entry and product, from the fragment's products."""
    found = {}
    for product in fragment.get('products') or []:
        base = product.get('base') if isinstance(product, dict) else None
        if not isinstance(base, dict) or not isinstance(base.get('types'), dict):
            continue
        for type_id, entry in base['types'].items():
            if not isinstance(entry, dict):
                continue
            # The default type, retail for World of Warcraft, takes the product's own UID.
            uid = entry.get('uid') or (base.get('uid') if type_id == base.get('default_product_type') else None)
            if isinstance(uid, str):
                found.setdefault(uid, (type_id, entry, {**base, 'id': product.get('id')}))
    return found


def _name(entry: dict, defaults: dict, base: dict, text: Callable[[object], str]) -> str:
    """The name Battle.net shows for a type, such as 'MoP Classic PTR'."""
    selector, name = text(entry.get('selector_display_name')), text(entry.get('name'))
    type_name = text(entry.get('type_name') or defaults.get('type_name'))
    suffixed = (entry.get('name_style') or defaults.get('name_style')) == 'type_name_suffix'
    if selector:
        return selector
    if name and type_name and suffixed:
        return f'{type_name} {name}'
    return name or text(base.get('name'))


def _modes(entry: dict, text: Callable[[object], str]) -> list[list]:
    """The game modes a type offers, each with the arguments that start it."""
    modes = []
    for package in entry.get('game_packages') or []:
        if not isinstance(package, dict) or package.get('launchable') is not True:
            continue
        arguments = package.get('launch_arguments')
        ident, label = package.get('id'), text(package.get('label'))
        # Only plain -name=value switches reach a command line.
        if (isinstance(ident, str) and UID.fullmatch(ident) and label and isinstance(arguments, list) and arguments
                and all(isinstance(a, str) and ARGUMENT.fullmatch(a) for a in arguments)):
            modes.append([ident, label, arguments])
    return modes


def parse(root: dict, fragment: dict) -> dict[str, dict]:
    """The catalog's World of Warcraft installs, keyed by product code."""
    strings = {**_strings(root), **_strings(fragment)}
    categories = {c['id']: c for c in _definitions(root, 'categories') + _definitions(fragment, 'categories')}
    defaults = {t['id']: t for t in _definitions(root, 'types') + _definitions(fragment, 'types')}
    types = _types(fragment)

    def text(key) -> str:
        return ' '.join(strings.get(key, '').split())[:80] if isinstance(key, str) else ''

    installs = fragment.get('installs')
    table: dict[str, dict] = {}
    for uid, install in sorted(installs.items() if isinstance(installs, dict) else []):
        code = install.get('tact_product') if isinstance(install, dict) else None
        if not catalog.is_product(code) or not UID.fullmatch(uid):
            continue
        type_id, entry, base = types.get(uid, ('', {}, {}))
        definition = defaults.get(type_id, {})
        product_defaults = definition.get('product_defaults') if isinstance(definition.get('product_defaults'), dict) else {}
        deprecated = install.get('deprecated') is True or entry.get('deprecated') is True
        live = categories.get(definition.get('category'), {}).get('name') == LIVE_CATEGORY
        if live and not deprecated:
            kind = catalog.LIVE
        elif TEST_MARKER.search(f'{type_id} {uid} {code}'):
            kind = catalog.TEST
        else:
            kind = catalog.OTHER
        row = {'uid': uid, 'name': _name(entry, product_defaults, base, text), 'kind': kind,
               'product': str(entry.get('override_product_id') or base.get('id') or ''),
               'deprecated': deprecated, 'modes': _modes(entry, text)}
        # A product with several UIDs keeps the one Battle.net still offers.
        if code not in table or (table[code]['deprecated'] and not deprecated):
            table[code] = row
    return table


def valid(table) -> bool:
    """A table as parse makes it, holding World of Warcraft itself. Anything else is not used."""
    if not isinstance(table, dict) or table.get('format') != FORMAT or not isinstance(table.get('checked_at'), int):
        return False
    installs = table.get('installs')
    if not isinstance(installs, dict) or 'wow' not in installs:
        return False
    for code, row in installs.items():
        if not catalog.is_product(code) or not isinstance(row, dict):
            return False
        if not isinstance(row.get('uid'), str) or not UID.fullmatch(row['uid']) or row.get('kind') not in KINDS:
            return False
        if not isinstance(row.get('name'), str) or not isinstance(row.get('product'), str):
            return False
        for mode in row.get('modes') if isinstance(row.get('modes'), list) else [None]:
            if (not isinstance(mode, list) or len(mode) != 3 or not all(isinstance(x, str) for x in mode[:2])
                    or not isinstance(mode[2], list) or not all(isinstance(a, str) and ARGUMENT.fullmatch(a)
                                                                for a in mode[2])):
                return False
    return True


# Fetching it

def _rows(get: Callable[[str], bytes], region: str) -> tuple[list[dict], dict]:
    """The catalog builds, newest format first, and the CDN for the region."""
    versions = [row for row in parse_psv(get(VERSIONS.format(region=region)))
                if PUBLISHED.fullmatch(row.get('Region', '')) and re.fullmatch(r'[0-9a-f]{32}', row.get('BuildConfig', ''))]
    versions.sort(key=lambda row: int(PUBLISHED.fullmatch(row['Region']).group(1)), reverse=True)
    cdns = [row for row in parse_psv(get(CDNS.format(region=region))) if row.get('Name')]
    # Taiwan has no CDN row of its own for the catalog. The Americas one serves the same files.
    cdn = next((row for row in cdns if row['Name'] == region), None) or \
        next((row for row in cdns if row['Name'] == 'us'), None)
    if not versions or cdn is None:
        raise ValueError('The version service names no catalog build')
    return versions, cdn


def _document(client: CDNClient, key: str) -> dict:
    document = json.loads(client.get_plain(key, LIMIT))
    if not isinstance(document, dict):
        raise ValueError('A catalog file is not a JSON object')
    return document


def _read(client: CDNClient, build: str) -> dict[str, dict]:
    config = parse_config(client.get_config(build))
    root = _document(client, config['root'][0])
    fragment = next((f.get('hash') for f in root.get('fragments') or []
                     if isinstance(f, dict) and f.get('name') == FRAGMENT and 'requires' not in f), None)
    if not isinstance(fragment, str):
        raise ValueError('The catalog has no World of Warcraft fragment')
    return parse(root, _document(client, fragment))


def fetch(region: str, cache: Path, *, get: Callable[[str], bytes]) -> dict:
    """Read the catalog from the CDN. Raises when no catalog format gives a usable table.

    A newer format Scuffle cannot read yet falls back to the one before it.
    """
    versions, cdn = _rows(get, region)
    with CDNClient(cache, cdn['Name'], 'catalogs', retries=1, timeout=20) as client:
        client.set_cdn(cdn)
        last: Exception = ValueError('No catalog build')
        for row in versions[:TRIES]:
            try:
                table = {'format': FORMAT, 'checked_at': int(time.time()),
                         'build': f"{row['Region']} {row.get('VersionsName', '')}".strip(),
                         'installs': _read(client, row['BuildConfig'])}
            except Exception as error:      # a changed format reads as a broken one, so try the older
                last = error
                continue
            if valid(table):
                return table
            last = ValueError(f"Catalog {row['Region']} has no usable World of Warcraft installs")
    raise last


# The cache

def path(cache: Path) -> Path:
    return Path(cache) / 'catalog' / 'battlenet.json'


def load(cache: Path) -> dict | None:
    """The last good table, or None. A damaged file counts as none."""
    try:
        table = json.loads(path(cache).read_text())
    except (OSError, ValueError):
        return None
    return table if valid(table) else None


def save(cache: Path, table: dict) -> None:
    from .library import atomic_json
    atomic_json(path(cache), table)


def fresh(table: dict | None, now: float | None = None) -> bool:
    if not table:
        return False
    age = (time.time() if now is None else now) - table['checked_at']
    return 0 <= age < TTL


def update(region: str, cache: Path, *, get: Callable[[str], bytes]) -> dict | None:
    """Read the catalog again once the cached one is old. Never raises.

    A failure keeps the cached table, so the next check tries again.
    """
    cached = load(cache)
    if fresh(cached):
        return cached
    try:
        table = fetch(region, cache, get=get)
    except Exception:       # offline, or the catalog changed. The last good one stays.
        return cached
    save(cache, table)
    return table


def current(cache: Path) -> dict:
    """The newer of the cached table and the snapshot Scuffle ships with."""
    from .catalog_snapshot import SNAPSHOT
    cached = load(cache)
    return cached if cached and cached['checked_at'] >= SNAPSHOT['checked_at'] else SNAPSHOT
