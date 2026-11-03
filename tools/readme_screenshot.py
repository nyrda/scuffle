"""Render the README's screenshot, docs/screenshot.png, from code.

Run inside the dev shell after a change to the window:
    nix develop -c python3 tools/readme_screenshot.py

The state is made up in memory, as in tools/screens.py: a few installed clients, a signed-in
account and an update in the download bar. Nothing is installed, downloaded or saved, and the
client catalog comes from tests/fixtures/catalog-us.json, so the picture only changes when the
window does.
"""
from datetime import datetime
import os
from pathlib import Path
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['QT_SCALE_FACTOR'] = '2'         # sharp on high-density screens and when GitHub scales it
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import screens                              # noqa: E402, its own throwaway folders come first

from PySide6.QtWidgets import QApplication  # noqa: E402

from scuffle import catalog, discovery     # noqa: E402
from scuffle.downloads import QUEUED, RUNNING, Job  # noqa: E402
from scuffle.settings import Settings      # noqa: E402

OUTPUT = Path(__file__).parent.parent / 'docs/screenshot.png'
SIZE = (1180, 720)
DAY = int(datetime(2026, 10, 29, 20, 0).timestamp())
INSTALLED = {      # client: (folder, size in bytes)
    'wow': ('_retail_', 133_100_000_000),
    'wow_classic': ('_classic_', 81_400_000_000),
    'wow_classic_era': ('_classic_era_', 31_800_000_000),
    'wowf': ('_forever_', 91_200_000_000),
}


def main() -> None:
    Settings.save = lambda self: None
    fixture = screens.json.loads((Path(__file__).parent.parent / 'tests/fixtures/catalog-us.json').read_text())
    discovery.load = lambda cache, region: fixture | {'region': region, 'checked_at': DAY}
    application = QApplication(sys.argv)
    screens.ui.prepare(application)
    window = screens.ui.MainWindow()
    window.check_updates = lambda: None
    window.free_space = lambda: 1_240_000_000_000     # the same on every machine
    screens.fake_installed(window, 'wow')       # an installation folder in the throwaway home
    window.settings.region = 'us'
    window.settings.hidden_clients = []
    window.library.installed = lambda: list(INSTALLED)
    window.library.state = lambda code: {
        'version': catalog.release(code).version, 'region': 'us', 'locale': 'enUS',
        'folder': INSTALLED[code][0], 'encoded_bytes': INSTALLED[code][1], 'updated_at_unix': DAY,
    } if code in INSTALLED else None
    window.settings.last_played = {code: DAY for code in INSTALLED}
    window.on_storage({'stored': 250_900_000_000, 'clients': {code: size for code, (_, size) in INSTALLED.items()}})
    login = screens.fake_login(window.vault(), 1)
    login.save_battle_tag('Adventurer#1234')
    login.save_accounts(1, [{'id': 11, 'name': 'ADVENTURER', 'region': 'US'}])
    # Classic Era updates in the bar below while World of Warcraft is ready to play.
    job = Job('wow_classic_era', 'update', status=RUNNING, stage='downloading',
              done_bytes=2_300_000_000, total_bytes=5_900_000_000, speed=48_000_000)
    window.downloads.jobs[:] = [job, Job('wow_classic', 'update', status=QUEUED, total_bytes=1_200_000_000)]
    window.downloads._current = job
    window.clear_notice('')
    window.populate()
    window.client_list.select('wow')
    window.resize(*SIZE)
    window.show()
    window.refresh()
    for _ in range(5):
        application.processEvents()
    # A plain example instead of the throwaway folder's temporary path.
    window.client_page.storage.path.setText('~/Games/Scuffle')
    application.processEvents()
    OUTPUT.parent.mkdir(exist_ok=True)
    window.grab().save(str(OUTPUT))
    window.downloads.jobs.clear()
    window.downloads._current = None
    window.close()
    print(f'Wrote {OUTPUT}')


if __name__ == '__main__':
    main()
