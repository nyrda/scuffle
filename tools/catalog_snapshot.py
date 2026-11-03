"""Write src/scuffle/catalog_snapshot.py from Battle.net's public catalog.

The snapshot is what a first start without a network lists. Run it when Blizzard
adds or renames a client, and before a release:

    nix develop -c env PYTHONPATH=src python3 tools/catalog_snapshot.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

# Without a snapshot module catalog cannot import, so a placeholder stands in while this runs.
TARGET = ROOT / 'src' / 'scuffle' / 'catalog_snapshot.py'
if not TARGET.exists():
    TARGET.write_text("SNAPSHOT = {'format': 1, 'checked_at': 0, 'build': '', 'installs': {}}\n")

from scuffle import battlenet, discovery     # noqa: E402

HEADER = '''"""Battle.net's World of Warcraft installs, as tools/catalog_snapshot.py read them.

Used until Scuffle has read the catalog itself, so a first start without a network
still lists the clients. Generated, do not edit.
"""
'''


def source(table: dict) -> str:
    """The table as Python, one install to a line, so a change shows as a small diff."""
    lines = ['SNAPSHOT = {', *(f'    {key!r}: {table[key]!r},' for key in ('format', 'checked_at', 'build')),
             "    'installs': {"]
    lines += [f'        {code!r}: {row!r},' for code, row in sorted(table['installs'].items())]
    return '\n'.join(lines + ['    },', '}', ''])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--region', default='us')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as cache:
        table = battlenet.fetch(args.region, Path(cache), get=discovery.fetch)
    if not battlenet.valid(table):
        raise SystemExit('The catalog gave no usable table')
    TARGET.write_text(HEADER + '\n' + source(table))
    print(f"{TARGET.relative_to(ROOT)}: {len(table['installs'])} installs from {table['build']}, "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(table['checked_at']))} UTC")


if __name__ == '__main__':
    main()
