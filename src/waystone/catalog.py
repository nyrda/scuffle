"""The public World of Warcraft clients Waystone lists.

Labels, install UIDs and modes come from Battle.net's catalog. Folder names
and extra launch arguments come from each client's public product
configuration at install time.
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
    parent: str = ''     # the public client a test client belongs to
    test: bool = False   # PTRs and betas, hidden unless test clients are shown
    modes: tuple[Mode, ...] = field(default_factory=tuple)

    def launch_arguments(self, mode: str | None = None, extra: list[str] | None = None) -> list[str]:
        arguments = ['-launcherlogin', '-uid', self.uid, *(extra or [])]
        if mode:
            selected = next((m for m in self.modes if m.id == mode), None)
            if selected is None:
                raise ValueError(f'{self.label} has no mode {mode!r}')
            arguments.append(selected.argument)
        return arguments


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
}

CLIENTS = (
    Client('wow', 'World of Warcraft', 'wow', 'retail', 'WoW'),
    Client('wow_classic', 'Mists of Pandaria Classic', 'wow_classic', 'classic', 'MoP'),
    Client('wow_anniversary', 'Burning Crusade Anniversary', 'wow_classic_anniversary', 'classic', 'TBC'),
    Client('wow_classic_era', 'Classic Era', 'wow_classic_era', 'classic', 'Era', modes=ERA_MODES),
    # Forever's release channel is not published yet. Its stress test is.
    Client('wowf', 'Forever', 'wowf', 'forever', 'FV'),
    Client('wowt', 'Retail PTR', 'wow_ptr', 'retail', 'PTR', parent='wow', test=True),
    Client('wowxptr', 'Retail PTR 2', 'wow_ptr_x', 'retail', 'PTR2', parent='wow', test=True),
    Client('wow_beta', 'Retail Beta', 'wow_beta', 'retail', 'β', parent='wow', test=True),
    Client('wow_classic_ptr', 'MoP Classic PTR', 'wow_classic_ptr', 'classic', 'PTR', parent='wow_classic', test=True),
    Client('wow_classic_era_ptr', 'Classic Era PTR', 'wow_classic_era_ptr', 'classic', 'PTR', parent='wow_classic_era', test=True,
           modes=ERA_MODES),
    Client('wow_classic_beta', 'Forever Beta', 'wow_classic_beta', 'forever', 'β', parent='wowf', test=True),
)

CLIENTS = tuple(Client(**{**c.__dict__, 'blurb': BLURBS.get(c.code, '')}) for c in CLIENTS)
BY_CODE = {client.code: client for client in CLIENTS}


def visible(extra: set[str] | list[str] = (), installed: set[str] | list[str] = ()) -> list[Client]:
    """Clients for the list: public ones, chosen test clients and anything installed."""
    shown = set(extra) | set(installed)
    return [client for client in CLIENTS if not client.test or client.code in shown]


def get(code: str) -> Client:
    try:
        return BY_CODE[code]
    except KeyError:
        raise ValueError(f'Unknown client {code!r}') from None
