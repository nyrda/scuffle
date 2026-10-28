"""Render the window's pages and states to PNG files for design review.

Run inside the dev shell:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 tools/screens.py OUTPUT_DIR

States are simulated in memory. Nothing is installed, downloaded, launched or saved.
The client catalog comes from tests/fixtures/catalog-us.json, so no state depends on
what Blizzard publishes today.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

# Folders of its own, so no state reads or touches the user's settings, sign-in or library.
_home = tempfile.mkdtemp(prefix='waystone-screens-')
for _name in ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'APPDATA', 'LOCALAPPDATA']:
    os.environ[_name] = os.path.join(_home, _name.lower())

from PySide6.QtWidgets import QApplication, QScrollArea

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
    fake_installed(window, 'wow')               # one installed client in a throwaway folder
    window.clear_notice('')
    window.library.installed = lambda: ['wow']
    window.clear_notice('')                     # the folder notice from before it was chosen
    window.settings.region = 'us'               # the region the catalog fixture was captured for
    window.on_storage(window.library.storage() if window.library else {})
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.show()

    def shot(name, arrange=None, reveal=None):
        for size, (width, height) in SIZES.items():
            window.resize(width, height)
            window.refresh()
            for _ in range(3):
                application.processEvents()
            if arrange:             # after the layout, such as scrolling to the end
                arrange()
                for _ in range(3):
                    application.processEvents()
            if reveal is not None:
                # Scrolled after the resize, so the part under review is in view at every size.
                scroll = reveal.parentWidget()
                while scroll is not None and not isinstance(scroll, QScrollArea):
                    scroll = scroll.parentWidget()
                scroll.ensureWidgetVisible(reveal, 0, 40)
                application.processEvents()
            window.grab().save(str(output / f'{name}-{size}.png'))

    window.updates.clear()
    window.client_list.select('wow')
    shot('01-installed-ready')
    window.storage['clients']['wow'] = 92_400_000_000
    window.open_uninstall()
    shot('01b-uninstall-confirmation')
    window.uninstall_page.keep.setChecked(False)
    shot('01c-uninstall-delete-settings')
    window.show_client_page()
    window.client_list.select('wow_classic_era')
    shot('02-not-installed')
    if window.downloads:
        job = Job('wow_classic_era', 'install', status=RUNNING, stage='downloading',
                  done_bytes=12_400_000_000, total_bytes=31_800_000_000, speed=48_000_000)
        queued = Job('wow_classic', 'install', status=QUEUED, total_bytes=8_000_000_000)
        window.downloads.jobs.extend([job, queued])
        window.downloads._current = job
        shot('03-downloading')
        job.speed = 0
        shot('03f-estimating')
        job.speed = 48_000_000
        # Another client selected while Mists of Pandaria Classic downloads and Classic Era waits.
        job.client, queued.client = 'wow_classic', 'wow_classic_era'
        window.client_list.select('wow')
        shot('03c-downloading-elsewhere')
        window.client_list.select('wow_classic_era')
        shot('03e-queued-client')
        job.client, queued.client = 'wow_classic_era', 'wow_classic'
        window.client_list.select('wow_classic_era')
        window.downloads.jobs.append(Job('wowf', 'install', status=QUEUED, total_bytes=5_000_000_000))
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
        window.downloads.jobs[:] = [job, Job('wow_classic', 'install', status=PAUSED)]
        shot('04c-restored-client')
        window.open_queue()
        shot('04d-restored-queue')
        window.show_client_page()
        window.downloads.jobs.clear()
        failed = Job('wow_classic_era', 'install', status=FAILED,
                     error='Blizzard\'s download servers did not answer. Check the connection and try again.')
        window.downloads.jobs.append(failed)
        window.on_job(failed)
        shot('04b-failed')
        window.downloads.jobs.clear()
        window.clear_notice('wow_classic_era')
    window.client_list.select('wow')
    window.updates['wow'] = True
    shot('05-update-available')
    window.updates['wow'] = False
    window.phases['wow'] = 'Connecting'
    shot('06-launching')
    window.phases['wow'] = 'Running'
    shot('07-running')
    window.phases.clear()
    signed_in = window.signed_in
    window.signed_in = lambda: False
    window.notify('Battle.net did not accept the saved sign-in.', 'error', client='wow')
    shot('08-error-banner')
    window.clear_notice('wow')
    window.signed_in = signed_in
    # A start that failed while another client was selected shows in the list.
    window.launch_failed.add('wowf')
    window.notify('Forever did not start. Check its runtime and try again.', 'error', client='wowf')
    shot('08b-error-elsewhere')
    window.launch_failed.discard('wowf')
    window.clear_notice('wowf')
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
    # The three categories, with the other public clients shown and one download.
    window.settings.show_other_clients = True
    window.settings.extra_clients = ['wow_classic_era_ptr', 'wowlivetest']
    window.settings_page.load(window.settings, [], {'wow_classic_era_ptr': 'Queued'})
    window.populate()
    shot('10d-settings-clients-other')
    shot('10e-settings-clients-other-end', lambda: window.settings_page.scroll.verticalScrollBar().setValue(10_000))
    window.settings_page.show_section('downloads')
    window.settings_page.limit.setValue(37)
    window.settings_page.workers.setValue(8)
    window.settings_page.pause_while_playing.setChecked(True)
    window.settings_page.resume_downloads.setChecked(True)
    shot('10f-settings-downloads')
    window.settings_page.show_section('launch')
    shot('10g-settings-launch')
    window.settings_page.show_section('general')
    window.downloads.jobs.clear() if window.downloads else None
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.settings_page.lock_folder('')
    window.open_settings()
    folder = Path(_home) / 'games' / 'Waystone'
    folder.mkdir(parents=True)
    window.open_folder_picker(str(folder))
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
    launch_states(window, shot)
    from waystone.ui import pages
    pages.open_browser = lambda url: None
    window.signed_in = lambda: False             # the sign-in page only appears without a session
    window.on_sign_in_requested('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    shot('11-sign-in')
    print(f'Wrote {len(list(output.glob("*.png")))} screenshots to {output}')


def launch_states(window, shot) -> None:
    """The Linux launch settings and one client's launch options, fine and with mistakes."""
    from waystone.launch_options import ClientOptions
    page = window.settings_page
    if page.launch is not None:
        window.open_settings('launch')
        shot('20-launch-settings', reveal=page.launch.environment)
        launch = page.launch
        launch.boxes['gamemode'].setChecked(True)
        launch.boxes['log'].setChecked(True)
        launch.wrapper.setText('gamescope-not-installed -f --')
        launch.environment.text.setPlainText('DXVK_HUD=fps\nVKD3D CONFIG=dxr')
        shot('20b-launch-settings-mistakes', reveal=launch.environment.problem)
        window.settings_page.load(window.settings, *window.kept_split())
    code = 'wow'
    if not (window.library and window.library.state(code)):
        fake_installed(window, code)
    window.client_list.select(code)
    window.show_client_page()
    window.open_launch_options()
    shot('21-launch-options-empty')
    options = window.options_page
    options.load(ui.catalog.get(code), ClientOptions(graphics='-d3d11', switches=['-console'],
                                                     arguments='-config Config2.wtf',
                                                     environment={'DXVK_HUD': 'fps'}))
    shot('21b-launch-options')
    options.arguments.setText('-config Config2.wtf -d3d12')
    shot('21d-launch-options-overlap', reveal=options.arguments_note)
    options.arguments.setText('-console -uid wow')
    if options.environment:
        options.environment.text.setPlainText('DXVK_HUD=fps\n1BAD=yes')
    shot('21c-launch-options-mistakes', reveal=options.environment.problem if options.environment else None)
    window.settings.client_launch[code] = ClientOptions(graphics='-d3d11', switches=['-console'],
                                                        environment={'DXVK_HUD': 'fps'})
    options.load(ui.catalog.get(code), window.settings.client_launch[code])
    window.show_client_page()
    shot('22-client-with-launch-options')
    window.settings.client_launch.pop(code)


def fake_installed(window, code: str) -> None:
    """Show a client as installed, as the tests do, when this machine has none. Nothing is written."""
    import tempfile
    if not window.library:
        window.settings.root = tempfile.mkdtemp(prefix='waystone-screens-')
        window.open_library()
    state = window.library.state
    window.library.state = lambda c: ({'version': '12.0.5.66741', 'region': 'eu', 'locale': 'enUS',
                                       'folder': '_retail_'} if c == code else state(c))


if __name__ == '__main__':
    main(Path(sys.argv[1] if len(sys.argv) > 1 else 'screens'))
