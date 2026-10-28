"""The World of Warcraft clients Waystone lists.

Built-in entries cover the live and test clients. Their labels, install UIDs and
modes come from Battle.net's catalog. Blizzard's version service adds versions
and other public products at run time, see discovery.py. Folder names and extra
launch arguments come from each client's public product configuration at
install time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

LIVE, TEST, OTHER = 'live', 'test', 'other'
FAMILIES = ('retail', 'classic', 'forever')     # the selector's order
PRODUCT = re.compile(r'wow[a-z0-9_]{0,40}')


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    argument: str


@dataclass(frozen=True)
class Client:
    code: str            # NGDP product code, also the .flavor.info value
    label: str
    uid: str             # install UID passed as -uid
    family: str          # groups related clients in the selector
    short: str = ''      # tile text in the selector
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
            arguments.append(selected.argument)
        return arguments


@dataclass(frozen=True)
class Release:
    """What the version service publishes for a client in the chosen region."""
    version: str
    public: bool = True           # its build configuration and install manifest need no keys
    locales: tuple[str, ...] = ()  # empty when not checked, as for the built-in clients


ERA_MODES = (
    Mode('era', 'Era', '-initialgamemode=cera'),
    Mode('hardcore', 'Hardcore', '-initialgamemode=chc'),
    Mode('sod', 'Season of Discovery', '-initialgamemode=csod'),
)

BLURBS = {
    'wow': 'The current expansion of World of Warcraft.',
    'wow_classic': 'Classic progression, now in Mists of Pandaria.',
    'wow_anniversary': 'The Burning Crusade on fresh anniversary realms.',
    'wow_classic_era': 'The original game, with Hardcore and Season of Discovery realms.',
    'wowf': 'World of Warcraft: Forever. This is its stress test until the release.',
    'wowt': 'Test realm for the next retail patch.',
    'wowxptr': 'A second retail test realm, used for larger changes.',
    'wow_beta': 'Beta of the next retail expansion.',
    'wow_classic_ptr': 'Test realm for the next Classic patch.',
    'wow_classic_era_ptr': 'Test realm for Classic Era and its seasons.',
    'wow_classic_beta': 'Beta of World of Warcraft: Forever.',
    'wowlivetest': 'Blizzard\'s Dark Realm test build. It was last published for Dragonflight.',
    'wowe1': 'A retail event build. It was last published for Shadowlands.',
    'wowz': 'A Classic submission build. It was last published for Classic Era 1.14.',
    'wow_cn_beta': 'The Forever beta for China, in Simplified Chinese only.',
}

CLIENTS = (
    Client('wow', 'World of Warcraft', 'wow', 'retail', 'WoW'),
    Client('wow_classic', 'Mists of Pandaria Classic', 'wow_classic', 'classic', 'MoP'),
    Client('wow_anniversary', 'Burning Crusade Anniversary', 'wow_classic_anniversary', 'classic', 'TBC'),
    Client('wow_classic_era', 'Classic Era', 'wow_classic_era', 'classic', 'Era', modes=ERA_MODES),
    # Forever's release channel is not published yet. Its stress test is.
    Client('wowf', 'Forever', 'wowf', 'forever', 'FV'),
    Client('wowt', 'Retail PTR', 'wow_ptr', 'retail', 'PTR', parent='wow', category=TEST),
    Client('wowxptr', 'Retail PTR 2', 'wow_ptr_x', 'retail', 'PTR2', parent='wow', category=TEST),
    Client('wow_beta', 'Retail Beta', 'wow_beta', 'retail', 'β', parent='wow', category=TEST),
    Client('wow_classic_ptr', 'MoP Classic PTR', 'wow_classic_ptr', 'classic', 'PTR', parent='wow_classic',
           category=TEST),
    Client('wow_classic_era_ptr', 'Classic Era PTR', 'wow_classic_era_ptr', 'classic', 'PTR', parent='wow_classic_era',
           category=TEST, modes=ERA_MODES),
    Client('wow_classic_beta', 'Forever Beta', 'wow_classic_beta', 'forever', 'β', parent='wowf', category=TEST),
)

# Other products Waystone has a name for. They are listed only once discovery finds
# them public. Battle.net's catalog has no install UID for them, so it is the code,
# as for most built-in clients.
OTHERS = (
    Client('wowlivetest', 'Dark Realm Test', 'wowlivetest', 'retail', 'DRT', parent='wow', category=OTHER),
    Client('wowe1', 'Retail Event', 'wowe1', 'retail', 'EV', parent='wow', category=OTHER),
    Client('wowz', 'Classic Submission', 'wowz', 'classic', 'SUB', parent='wow_classic_era', category=OTHER),
    Client('wow_cn_beta', 'Forever China Beta', 'wow_cn_beta', 'forever', 'CN', parent='wowf', category=OTHER),
)

CLIENTS, OTHERS = (tuple(Client(**{**c.__dict__, 'blurb': BLURBS.get(c.code, '')}) for c in group)
                   for group in (CLIENTS, OTHERS))
BY_CODE = {client.code: client for client in CLIENTS}
NAMED_OTHERS = {client.code: client for client in OTHERS}

# What discovery found last, for the chosen region. Replaced as a whole, so a
# worker thread reading it never sees half an update.
_releases: dict[str, Release] = {}
_discovered: dict[str, Client] = {}


def is_product(code) -> bool:
    """A World of Warcraft product code, which is also safe as a folder name."""
    return isinstance(code, str) and PRODUCT.fullmatch(code) is not None


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


def describe(code: str, version: str = '') -> Client:
    """A client for a product Waystone has no name for, named by its code and version."""
    known = NAMED_OTHERS.get(code)
    if known:
        return known
    family, parent = lineage(version) if version else ('retail', '')
    numbers = '.'.join(map(str, patch(version)))
    label = f'{LINE_NAMES[parent]} {numbers} ({code})' if numbers else code
    short = re.sub(r'^wow_?', '', code).replace('_', '')[:3].upper() or 'WoW'
    return Client(code, label, code, family, short, f'Published by Blizzard as {code}.', parent, OTHER)


def apply(products: dict[str, dict] | None) -> None:
    """Use what discovery found, or only the built-in clients when it found nothing."""
    global _releases, _discovered
    releases, discovered = {}, {}
    for code, row in (products or {}).items():
        if not is_product(code) or not isinstance(row, dict) or not row.get('version'):
            continue
        release = Release(str(row['version']), row.get('public', code in BY_CODE) is True,
                          tuple(row.get('locales') or ()))
        releases[code] = release
        if code not in BY_CODE and release.public:
            discovered[code] = describe(code, release.version)
    _releases, _discovered = releases, discovered


def checked() -> bool:
    """Whether versions from the version service are known."""
    return bool(_releases)


def release(code: str) -> Release | None:
    return _releases.get(code)


def clients(locale: str = '') -> list[Client]:
    """The built-in clients, then the other public products discovery found.

    With a locale, other products that cannot be installed in it are left out.
    """
    others = [c for c in _discovered.values()
              if not locale or not _releases[c.code].locales or locale in _releases[c.code].locales]
    return list(CLIENTS) + sorted(others, key=lambda c: (FAMILIES.index(c.family), c.label))


def get(code: str) -> Client:
    client = BY_CODE.get(code) or _discovered.get(code)
    if client:
        return client
    if is_product(code):
        # Installed, or chosen earlier, but not found this time. It still needs a name.
        found = _releases.get(code)
        return describe(code, found.version if found else '')
    raise ValueError(f'Unknown client {code!r}')


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
    """
    pool = list(clients() if clients_ is None else clients_)
    codes = {c.code for c in pool}
    pool += [get(code) for code in sorted(set(installed) | set(downloading)) if code not in codes and is_product(code)]
    shown = []
    for client in pool:
        if client.code in downloading:
            shown.append(client)
        elif client.code in hidden or (client.category == OTHER and not show_other):
            continue
        elif client.category == LIVE or client.code in extra or client.code in installed:
            shown.append(client)
    return shown
