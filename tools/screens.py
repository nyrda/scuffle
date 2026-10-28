"""Render the window's pages and states to PNG files for design review.

Run inside the dev shell:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 tools/screens.py OUTPUT_DIR

States are simulated in memory. Nothing is installed, downloaded, launched or saved.
The client catalog comes from tests/fixtures/catalog-us.json, so no state depends on
what Blizzard publishes today.
"""
import json
from pathlib import Path
import sys
import time

from PySide6.QtWidgets import QApplication

from waystone import discovery
from waystone.downloads import FAILED, PAUSED, QUEUED, RUNNING, Job
from waystone.settings import Settings
from waystone.ui import app as ui

SIZES = {'minimum': (900, 560), 'small': (960, 650), 'large': (1440, 900)}


def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    Settings.save = lambda self: None          # never write the user's settings
    fixture = json.loads((Path(__file__).parent.parent / 'tests/fixtures/catalog-us.json').read_text())
    discovery.load = lambda cache, region: fixture | {'region': region, 'checked_at': int(time.time())}
    application = QApplication(sys.argv)
    ui.prepare(application)
    window = ui.MainWindow()
    window.check_updates = lambda: None
    window.on_storage(window.library.storage() if window.library else {})
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.show()

    def shot(name, arrange=None):
        for size, (width, height) in SIZES.items():
            window.resize(width, height)
            window.refresh()
            for _ in range(3):
                application.processEvents()
            if arrange:             # after the layout, such as scrolling to the end
                arrange()
                for _ in range(3):
                    application.processEvents()
            window.grab().save(str(output / f'{name}-{size}.png'))

    window.updates.clear()
    window.client_list.select('wow')
    shot('01-installed-ready')
    window.client_list.select('wow_classic_era')
    shot('02-not-installed')
    if window.downloads:
        job = Job('wow_classic_era', 'install', status=RUNNING, stage='downloading',
                  done_bytes=12_400_000_000, total_bytes=31_800_000_000, speed=48_000_000)
        queued = Job('wow_classic', 'install', status=QUEUED)
        window.downloads.jobs.extend([job, queued])
        window.downloads._current = job
        shot('03-downloading')
        # Another client selected while Mists of Pandaria Classic downloads and Classic Era waits.
        job.client, queued.client = 'wow_classic', 'wow_classic_era'
        window.client_list.select('wow')
        shot('03c-downloading-elsewhere')
        window.client_list.select('wow_classic_era')
        shot('03e-queued-client')
        job.client, queued.client = 'wow_classic_era', 'wow_classic'
        window.client_list.select('wow_classic_era')
        window.open_queue()
        shot('03b-queue')
        window.show_client_page()
        preparing = Job('wow_classic_era', 'install', status=RUNNING, stage='planning')
        window.downloads.jobs[:] = [preparing]
        window.downloads._current = preparing
        shot('03d-preparing')
        job.status = PAUSED
        window.downloads.jobs[:] = [job]
        window.downloads._current = None
        shot('04-paused')
        window.downloads.jobs.clear()
        failed = Job('wow_classic_era', 'install', status=FAILED,
                     error='Blizzard\'s download servers did not answer. Check the connection and try again.')
        window.downloads.jobs.append(failed)
        window.on_job(failed)
        shot('04b-failed')
        window.downloads.jobs.clear()
        window.clear_notice('wow_classic_era')
    window.client_list.select('wowf')
    window.updates['wowf'] = True
    shot('05-update-available')
    window.updates['wowf'] = False
    window.phases['wowf'] = 'Connecting'
    shot('06-launching')
    window.phases['wowf'] = 'Running'
    shot('07-running')
    window.phases.clear()
    signed_in = window.signed_in
    window.signed_in = lambda: False
    window.notify('Battle.net did not accept the saved sign-in.', 'error', client='wowf')
    shot('08-error-banner')
    window.clear_notice('wowf')
    window.signed_in = signed_in
    window.client_list.select('wow_classic_era_ptr')
    shot('09-test-client')
    window.settings.extra_clients = [c.code for c in ui.catalog.CLIENTS if c.test]
    window.populate()
    shot('09b-all-test-clients')
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.open_settings()
    shot('10-settings')
    tests = [c.code for c in ui.catalog.CLIENTS if c.test]
    window.settings.extra_clients = tests          # installed test clients are always listed
    window.populate()
    ptr = Job('wow_classic_era_ptr', 'install', status=QUEUED)       # one test client waiting to download
    window.downloads.jobs.append(ptr) if window.downloads else None
    window.populate()
    window.settings_page.load(window.settings, [], {'wow_classic_era_ptr': 'Queued'})
    window.settings_page.lock_folder('Finish or cancel the downloads to choose another folder.')
    window.settings_page.show_section('clients')
    shot('10c-settings-installed-tests')
    # The three categories, with the other public clients shown, one installed client and one download.
    window.settings.show_other_clients = True
    window.settings.extra_clients = ['wow_classic_era_ptr', 'wowlivetest']
    window.settings_page.load(window.settings, ['wow'], {'wow_classic_era_ptr': 'Queued'})
    window.populate()
    shot('10d-settings-clients-other')
    shot('10e-settings-clients-other-end', lambda: window.settings_page.scroll.verticalScrollBar().setValue(10_000))
    window.settings_page.show_section('downloads')
    shot('10f-settings-downloads')
    window.settings_page.show_section('launch')
    shot('10g-settings-launch')
    window.settings_page.show_section('general')
    window.downloads.jobs.clear() if window.downloads else None
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.settings_page.lock_folder('')
    window.open_settings()
    window.open_folder_picker('/mnt/games/Waystone')
    # The folder tree loads in the background. Give it the time a person would see.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    shot('10b-folder')
    window.show_client_page()
    window.settings.show_other_clients = True
    window.settings.extra_clients = ['wow_classic_era_ptr', 'wowlivetest']
    window.populate()
    window.client_list.select('wowlivetest')
    shot('12-other-client')
    hidden = window.settings.hidden_clients
    window.settings.hidden_clients = [c.code for c in ui.catalog.clients()]
    window.populate()
    shot('13-every-client-hidden')
    window.settings.hidden_clients = hidden
    window.settings.show_other_clients = False
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.client_list.select('wow')
    from waystone.ui import pages
    pages.open_browser = lambda url: None
    window.signed_in = lambda: False             # the sign-in page only appears without a session
    window.on_sign_in_requested('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    shot('11-sign-in')
    print(f'Wrote {len(list(output.glob("*.png")))} screenshots to {output}')


if __name__ == '__main__':
    main(Path(sys.argv[1] if len(sys.argv) > 1 else 'screens'))
