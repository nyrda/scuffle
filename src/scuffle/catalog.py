"""The World of Warcraft clients Scuffle lists.

The list comes from Blizzard's data at run time. Battle.net's public catalog, see
battlenet.py, names each product's install UID, its name, whether Battle.net offers it
as a live game and its game modes. The version service, see discovery.py, gives each
product's build for the region and finds the products that download without a key.
Folder names and extra launch arguments come from each client's public product
configuration at install time.

The tables here only add what that data does not hold, for the games Scuffle knows:
the family the selector groups by, a short description, the preferred order, whether
a build that is not live is a test or an other client, and Classic Era's modes where
the catalog has none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

LIVE, TEST, OTHER = 'live', 'test', 'other'
CATEGORIES = (LIVE, TEST, OTHER)
FAMILIES = ('retail', 'classic', 'forever')     # the selector's order
PRODUCT = re.compile(r'wow[a-z0-9_]{0,40}')


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class Client:
    code: str            # NGDP product code, also the .flavor.info value
    label: str
    uid: str             # install UID passed as -uid
    family: str          # groups related clients in the selector
    blurb: str = ''      # one line about what the client is
    parent: str = ''     # the live client a test or other client belongs to
    category: str = LIVE
    modes: tuple[Mode, ...] = field(default_factory=tuple)

    @property
    def test(self) -> bool:
        """PTRs and betas."""
        return self.category == TEST

    @property
    def optional(self) -> bool:
        """Listed only when chosen in the settings, or installed."""
        return self.category != LIVE

    def launch_arguments(self, mode: str | None = None, extra: list[str] | None = None) -> list[str]:
        arguments = ['-launcherlogin', '-uid', self.uid, *(extra or [])]
        if mode:
            selected = next((m for m in self.modes if m.id == mode), None)
            if selected is None:
                raise ValueError(f'{self.label} has no mode {mode!r}')
            arguments += selected.arguments
        return arguments


@dataclass(frozen=True)
class Release:
    """What the version service publishes for a client in the chosen region."""
    version: str
    public: bool = True           # its build configuration and install manifest need no keys
    locales: tuple[str, ...] = ()  # empty when not checked, as for the known clients


@dataclass(frozen=True)
class Known:
    """What Scuffle adds to Blizzard's data for a game it knows."""
    label: str
    family: str
    category: str = LIVE     # a test or other client, when Battle.net does not offer it as live
    parent: str = ''
    blurb: str = ''
    modes: tuple[Mode, ...] = ()        # used when the catalog names none


ERA_MODES = (
    Mode('era', 'Era', ('-initialgamemode=cera',)),
    Mode('hardcore', 'Hardcore', ('-initialgamemode=chc',)),
    Mode('sod', 'Season of Discovery', ('-initialgamemode=csod',)),
)
# Battle.net's game package ids, and the ids and short labels settings and shortcuts
# have always used for them.
MODE_NAMES = {'wow_classic_era_base': ('era', 'Era'), 'wow_classic_era_hardcore': ('hardcore', 'Hardcore'),
              'wow_classic_era_sod': ('sod', 'Season of Discovery')}
# Battle.net's product a type belongs to, for a client Scuffle does not know.
PRODUCT_FAMILIES = {'WoWC': 'classic', 'WoWF': 'forever'}

# In the preferred order. Live and test clients here are trusted to be public, so
# discovery does not check them. Other clients are listed once discovery finds them public.
KNOWN = {
    'wow': Known('World of Warcraft', 'retail', blurb='The current expansion of World of Warcraft.'),
    'wow_classic': Known('Mists of Pandaria Classic', 'classic',
                         blurb='Classic progression realms, a step behind the current expansion.'),
    'wow_anniversary': Known('Burning Crusade Anniversary', 'classic',
                             blurb='The Burning Crusade on fresh anniversary realms.'),
    'wow_classic_era': Known('Classic Era', 'classic', modes=ERA_MODES,
                             blurb='The original game, with Hardcore and Season of Discovery realms.'),
    'wowf': Known('Forever', 'forever', blurb='World of Warcraft: Forever.'),
    'wowt': Known('Retail PTR', 'retail', TEST, 'wow', 'Test realm for the next retail patch.'),
    'wowxptr': Known('Retail PTR 2', 'retail', TEST, 'wow', 'A second retail test realm, used for larger changes.'),
    'wow_beta': Known('Retail Beta', 'retail', TEST, 'wow', 'Beta of the next retail expansion.'),
    'wow_classic_ptr': Known('MoP Classic PTR', 'classic', TEST, 'wow_classic',
                             'Test realm for the next Classic patch.'),
    'wow_classic_era_ptr': Known('Classic Era PTR', 'classic', TEST, 'wow_classic_era',
                                 'Test realm for Classic Era and its seasons.', ERA_MODES),
    'wow_classic_beta': Known('Forever Beta', 'forever', TEST, 'wowf', 'Beta of World of Warcraft: Forever.'),
    'wowlivetest': Known('Dark Realm Test', 'retail', OTHER, 'wow',
                         'Blizzard\'s Dark Realm test build. It was last published for Dragonflight.'),
    'wowe1': Known('Retail Event', 'retail', OTHER, 'wow', 'A retail event build. It was last published for Shadowlands.'),
    'wowz': Known('Classic Submission', 'classic', OTHER, 'wow_classic_era',
                  'A Classic submission build. It was last published for Classic Era 1.14.'),
    'wow_cn_beta': Known('Forever China Beta', 'forever', OTHER, 'wowf',
                         'The Forever beta for China, in Simplified Chinese only.'),
}
ORDER = list(KNOWN)

# What Blizzard's data says now: the catalog's installs, what earlier catalogs said about
# products it no longer lists, the releases discovery found and the clients offered, in order. Replaced as a whole, so a worker thread reading
# it never sees half an update.
_state: tuple[dict[str, dict], dict[str, dict], dict[str, Release], dict[str, Client]] = ({}, {}, {}, {})


def is_product(code) -> bool:
    """A World of Warcraft product code, which is also safe as a folder name."""
    return isinstance(code, str) and PRODUCT.fullmatch(code) is not None


def trusted(code: str) -> bool:
    """A live or test client Scuffle knows, which needs no check that it downloads without a key."""
    return code in KNOWN and KNOWN[code].category != OTHER


def patch(version: str) -> tuple[int, ...]:
    """The first three numbers of a version, such as (12, 1, 5) for 12.1.5.70575."""
    parts = []
    for part in version.split('.')[:3]:
        if not part.isdigit():
            break
        parts.append(int(part))
    return tuple(parts)


def lineage(version: str) -> tuple[str, str]:
    """The family and the live client a build of this version belongs with."""
    numbers = patch(version) + (0, 0)
    major, minor = numbers[0], numbers[1]
    if major == 1 and minor >= 50:
        return 'forever', 'wowf'
    if major == 1:
        return 'classic', 'wow_classic_era'
    if major == 2:
        return 'classic', 'wow_anniversary'
    if 3 <= major <= 5:
        return 'classic', 'wow_classic'
    return 'retail', 'wow'


LINE_NAMES = {'wowf': 'Forever', 'wow_classic_era': 'Classic Era', 'wow_anniversary': 'Classic',
              'wow_classic': 'Classic', 'wow': 'Retail'}


def plain_name(name: str) -> str:
    """Battle.net's name without the game's own name in front, such as 'Forever' for 'WoW: Forever'."""
    name = re.sub(r'^(World of Warcraft|WoW)\s*:\s*', '', name.strip())
    return re.sub(r'\s+-\s+', ' ', name)


def _builtin() -> dict[str, dict]:
    from .catalog_snapshot import SNAPSHOT
    return SNAPSHOT['installs']


def _modes(entry: dict, known: Known | None) -> tuple[Mode, ...]:
    modes = []
    for ident, label, arguments in entry.get('modes', []):
        mine, short = MODE_NAMES.get(ident, (re.sub(r'[^a-z0-9-]', '-', ident), label))
        modes.append(Mode(mine, short, tuple(arguments)))
    return tuple(modes) or (known.modes if known else ())


def _category(code: str, entry: dict) -> str:
    """Battle.net decides whether a client is live. For one that is not, Scuffle's table
    says whether it is a test or an other client, and the catalog's markers otherwise.
    A live game the catalog no longer offers, or no longer lists, becomes an other client."""
    known, kind = KNOWN.get(code), entry.get('kind')
    if kind == LIVE:
        return LIVE
    if known and known.category != LIVE:
        return known.category
    return kind if kind in CATEGORIES else OTHER


def _describe(code: str, installs: dict, dropped: dict, releases: dict[str, Release]) -> Client:
    """A client before its parent is settled, from what is known about it."""
    known = KNOWN.get(code)
    entry = installs.get(code) or {}
    # A product the catalog dropped still launches with the install UID it had, and keeps its name.
    earlier = entry or dropped.get(code) or _builtin().get(code) or {}
    found = releases.get(code)
    version = found.version if found else ''
    category = _category(code, entry)
    if known:
        family, label, blurb = known.family, known.label, known.blurb
    else:
        family = PRODUCT_FAMILIES.get(earlier.get('product', '')) or (lineage(version)[0] if version else 'retail')
        numbers = '.'.join(map(str, patch(version)))
        label = plain_name(earlier.get('name', '')) or \
            (f'{LINE_NAMES[lineage(version)[1]]} {numbers} ({code})' if numbers else code)
        blurb = f'Published by Blizzard as {code}.'
    parent = known.parent if known else (lineage(version)[1] if version else '')
    return Client(code, label, earlier.get('uid') or code, family, blurb, '' if category == LIVE else parent,
                  category, _modes(earlier, known))


def _settle(clients: list[Client], installs: dict) -> list[Client]:
    """Point each test and other client at a live client that is offered, and keep labels apart."""
    live = [c for c in clients if c.category == LIVE]
    settled = []
    for client in clients:
        parent = client.parent
        if client.category != LIVE and parent not in {c.code for c in live if c.family == client.family}:
            # Its usual live client is gone, such as a game that moved to a new product.
            parent = next((c.code for c in live if c.family == client.family), '')
        settled.append(Client(**{**client.__dict__, 'parent': parent}))
    # Live clients keep the plain name, so a release under a new product reads as the game.
    taken: set[str] = set()
    named = {}
    for client in sorted(settled, key=lambda c: CATEGORIES.index(c.category)):
        label = client.label
        if label in taken:
            name = plain_name((installs.get(client.code) or {}).get('name', ''))
            label = name if name and name not in taken else f'{client.label} ({client.code})'
        taken.add(label)
        named[client.code] = label
    return [Client(**{**c.__dict__, 'label': named[c.code]}) for c in settled]


def _order(client: Client) -> tuple:
    known = ORDER.index(client.code) if client.code in KNOWN and client.category != OTHER else len(ORDER)
    return CATEGORIES.index(client.category), known, FAMILIES.index(client.family), client.label


def offered(code: str, installs: dict, releases: dict[str, Release]) -> bool:
    """A known live or test client while the catalog or the version service names it, and
    anything else once discovery found it public."""
    found = releases.get(code)
    if trusted(code):
        return code in installs or found is not None
    return found is not None and found.public


def apply(products: dict[str, dict] | None, installs: dict | None = None, dropped: dict | None = None) -> None:
    """Use what discovery found and the Battle.net catalog's installs, and what earlier
    catalogs said about the products it dropped.

    No products means no versions are known. No installs means the snapshot Scuffle ships with.
    """
    global _state
    installs = installs if isinstance(installs, dict) else _builtin()
    dropped = dropped if isinstance(dropped, dict) else {}
    releases = {}
    for code, row in (products or {}).items():
        if not is_product(code) or not isinstance(row, dict) or not row.get('version'):
            continue
        releases[code] = Release(str(row['version']), row.get('public', trusted(code)) is True,
                                 tuple(row.get('locales') or ()))
    codes = [code for code in KNOWN if offered(code, installs, releases)]
    codes += sorted(code for code in releases if code not in KNOWN and offered(code, installs, releases))
    clients = _settle([_describe(code, installs, dropped, releases) for code in codes], installs)
    _state = installs, dropped, releases, {client.code: client for client in sorted(clients, key=_order)}


def checked() -> bool:
    """Whether versions from the version service are known."""
    return bool(_state[2])


def release(code: str) -> Release | None:
    return _state[2].get(code)


def clients(locale: str = '') -> list[Client]:
    """The clients offered, live ones first.

    With a locale, clients that discovery found cannot be installed in it are left out.
    """
    _, _, releases, offered_ = _state

    def fits(client):
        found = releases.get(client.code)
        return not locale or not found or not found.locales or locale in found.locales
    return [client for client in offered_.values() if fits(client)]


def describe(code: str) -> Client:
    """A client that is not offered now, such as one installed earlier, by what is known about it."""
    installs, dropped, releases, offered_ = _state
    if code in offered_:
        return offered_[code]
    client = _describe(code, installs, dropped, releases)
    return _settle(list(offered_.values()) + [client], installs)[-1]


def get(code: str) -> Client:
    if is_product(code):
        return describe(code)
    raise ValueError(f'Unknown client {code!r}')


def other_game(code: str) -> str:
    """The version a test client runs when it is another game than its live client, such as a
    Classic Era PTR carrying a Burning Crusade build. Empty when it is the same game."""
    client = get(code)
    mine, live = release(code), release(client.parent) if client.parent else None
    if not client.test or not mine or not live or patch(mine.version)[:1] == patch(live.version)[:1]:
        return ''
    return '.'.join(map(str, patch(mine.version)))


def stale(code: str) -> bool:
    """A test or other client whose patch is behind the live client it belongs with."""
    client = get(code)
    mine, live = release(code), release(client.parent) if client.parent else None
    if client.category == LIVE or not mine or not live:
        return False
    return patch(mine.version) < patch(live.version)


def visible(clients_: list[Client] | None = None, *, extra=(), hidden=(), installed=(), downloading=(),
            show_other: bool = False) -> list[Client]:
    """Clients for the list.

    Live clients unless hidden, chosen or installed test clients unless hidden, the
    same for other clients while they are shown at all, and anything with a download.
    An installed other client stays unless it is hidden itself, since the switch is for
    finding new ones.
    """
    pool = list(clients() if clients_ is None else clients_)
    codes = {c.code for c in pool}
    pool += [get(code) for code in sorted(set(installed) | set(downloading)) if code not in codes and is_product(code)]
    shown = []
    for client in pool:
        if client.code in downloading:
            shown.append(client)
        elif client.code in hidden or (client.category == OTHER and not show_other and client.code not in installed):
            continue
        elif client.category == LIVE or client.code in extra or client.code in installed:
            shown.append(client)
    return shown


apply(None)
