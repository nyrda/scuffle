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
_home = tempfile.mkdtemp(prefix='scuffle-screens-')
for _name in ['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'APPDATA', 'LOCALAPPDATA']:
    os.environ[_name] = os.path.join(_home, _name.lower())
os.environ['HOME'] = _home          # so the Steam the menu finds is a stand-in, never the user's
# No bus, so no state shows a real notification or puts an icon in the user's tray.
os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + os.path.join(_home, 'no-bus')

from PySide6.QtWidgets import QApplication, QScrollArea

from scuffle import discovery
from scuffle.downloads import FAILED, PAUSED, QUEUED, RUNNING, Job, MoveTask
from scuffle.settings import Settings
from scuffle.ui import app as ui

SIZES = {'minimum': (900, 560), 'small': (960, 650), 'large': (1440, 900)}


def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    Settings.save = lambda self: None          # never write the user's settings
    fixture = json.loads((Path(__file__).parent.parent / 'tests/fixtures/catalog-us.json').read_text())
    discovery.load = lambda cache, region: fixture | {'region': region, 'checked_at': int(time.time())}
    application = QApplication(sys.argv)
    ui.prepare(application)
    window = ui.MainWindow()
    window.screens_output = output
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
    # Signed in with two WoW accounts: the game asks until one is picked in the sidebar.
    vault = window.vault()
    main = fake_login(vault, 1)
    main.save_battle_tag('Wanderer#7395')
    main.save_accounts(1, [{'id': 11, 'name': 'WANDERER', 'region': 'EU'}, {'id': 12, 'name': 'WoW1', 'region': 'EU'}])
    shot('01d-two-accounts-ask')
    window.settings.game_accounts = {'1': 11}
    shot('01e-two-accounts-chosen')
    menu = window.game_account_menu()
    menu.popup(window.game_account_button.mapToGlobal(window.game_account_button.rect().bottomLeft()))
    application.processEvents()
    menu.grab().save(str(output / '01f-account-menu.png'))
    menu.close()
    # The Battle.net account switcher with one account, then with a second one added without a BattleTag.
    battle_net_menu(window, application, output / '01g-battle-net-menu-one.png')
    second = fake_login(vault, 2)
    second.save_accounts(2, [{'id': 21, 'name': 'ALTACCOUNT', 'region': 'EU'}])
    shot('01h-second-battle-net-account')
    battle_net_menu(window, application, output / '01i-battle-net-menu-two.png')
    window.phases['wowf'] = 'Connecting'          # no switching while a game starts
    shot('01j-switcher-during-launch')
    window.phases.clear()
    for login in vault.logins():
        vault.forget(login)
    window.settings.game_accounts = {}
    shortcut_states(window, shot, output, application)
    window.storage['clients']['wow'] = 92_400_000_000
    window.open_uninstall()
    shot('01b-uninstall-confirmation')
    window.uninstall_page.keep.setChecked(False)
    shot('01c-uninstall-delete-settings')
    window.show_client_page()
    window.client_list.select('wow_classic_era')
    shot('02-not-installed')
    import_states(window, shot, application)
    window.client_list.select('wow_classic_era')
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
    diagnostics_states(window, shot, application)
    window.settings_page.show_section('general')
    # Notifications and the tray, without a system tray and with one.
    window.settings_page.set_tray_available(False)
    shot('10j-settings-general-no-tray', reveal=window.settings_page.tray_note)
    window.settings_page.set_tray_available(True)
    window.settings_page.keep_in_tray.setChecked(True)
    shot('10k-settings-general-tray', reveal=window.settings_page.keep_in_tray)
    window.downloads.jobs.clear() if window.downloads else None
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.settings_page.lock_folder('')
    window.open_settings()
    folder = Path(_home) / 'games' / 'Scuffle'
    folder.mkdir(parents=True)
    window.open_folder_picker(str(folder))
    # The folder tree loads in the background. Give it the time a person would see.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    shot('10b-folder')
    move_states(window, shot, application)
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
    tray_menu(window, application, output)
    from scuffle.ui import pages
    pages.open_browser = lambda url: None
    window.signed_in = lambda: False             # the sign-in page only appears without a session
    window.on_sign_in_requested('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    shot('11-sign-in')
    window.on_sign_in_finished(None)
    # Adding a second account while the browser is still signed in to the first one.
    window._signing_in = True
    vault = window.vault()
    fake_login(vault, 314159265).save_battle_tag('Wanderer#7395')
    window.on_sign_in_requested('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    window.sign_in_page.take('http://localhost:0/?ST=EU-0123abcd-314159265&flowTrackingId=', explicit=True)
    shot('11b-add-account-same-account')
    window.on_sign_in_finished(None)
    window._signing_in = False
    # Closed before Python exits, so the window's event filter goes before the widgets it reads.
    window.close()
    print(f'Wrote {len(list(output.glob("*.png")))} screenshots to {output}')


def diagnostics_states(window, shot, application) -> None:
    """Before a game wrote a Proton log, then with one and the report just copied."""
    from scuffle.launch import log_dir
    page = window.settings_page
    page.show_section('diagnostics')
    shot('10h-settings-diagnostics')
    log_dir().mkdir(parents=True, exist_ok=True)
    (log_dir() / 'steam-battlenet.log').write_text('wine: process exited normally\n')
    page.show_section('general')
    page.show_section('diagnostics')
    page.request_diagnostics()
    deadline = time.monotonic() + 20
    while page.collecting and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    shot('10i-settings-diagnostics-copied')


def shortcut_states(window, shot, output: Path, application) -> None:
    """The client menu with the app launcher and Steam entries, the notices they give, and their images."""
    from scuffle import steam
    from scuffle.ui import style
    (Path(_home) / '.local/share/Steam/userdata/5/config').mkdir(parents=True, exist_ok=True)
    menu = window.client_page.more.menu()
    window.client_page.more.setFocus()
    menu.aboutToShow.emit()
    menu.popup(window.client_page.more.mapToGlobal(window.client_page.more.rect().bottomLeft()))
    application.processEvents()
    menu.grab().save(str(output / '01k-client-menu.png'))
    menu.close()
    window.toggle_launcher()
    shot('01l-added-to-launcher')
    menu.aboutToShow.emit()
    menu.popup(window.client_page.more.mapToGlobal(window.client_page.more.rect().bottomLeft()))
    application.processEvents()
    menu.grab().save(str(output / '01m-client-menu-in-launcher.png'))
    menu.close()
    running = steam.Steam.running
    steam.Steam.running = lambda self: True
    window.toggle_steam()
    shot('01n-steam-running')
    steam.Steam.running = running
    window.toggle_launcher()
    window.clear_notice('wow')
    for code in ['wow', 'wow_classic', 'wow_anniversary', 'wow_classic_era', 'wowf', 'wowt', 'wowlivetest']:
        client = ui.catalog.get(code)
        for size in (48, 256):
            style.tile_image(client, size).save(str(output / f'30-icon-{code}-{size}.png'))
        style.capsule_image(client, 600, 900).save(str(output / f'31-steam-portrait-{code}.png'))
        style.capsule_image(client, 920, 430).save(str(output / f'32-steam-wide-{code}.png'))


def launch_states(window, shot) -> None:
    """The Linux launch settings and one client's launch options, fine and with mistakes."""
    from scuffle.launch_options import ClientOptions
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


def fake_login(vault, owner: int):
    """A signed-in Battle.net login in the throwaway data folder. Its ticket is a placeholder."""
    with vault.adding(owner) as login:
        login.session.write_bytes(b'placeholder')
    return login


def battle_net_menu(window, application, path: Path) -> None:
    window.refresh()
    menu = window.account_menu()
    menu.popup(window.account_button.mapToGlobal(window.account_button.rect().bottomLeft()))
    application.processEvents()
    menu.grab().save(str(path))
    menu.close()


def fake_battle_net(folder: Path, clients: dict[str, str]) -> Path:
    """A Battle.net-style installation with empty content, only for the import page to find."""
    (folder / 'Data/data').mkdir(parents=True)
    (folder / 'Data/data/0000000001.idx').write_bytes(b'')
    (folder / '.build.info').write_text('Branch!STRING:0|Product!STRING:0\n' +
                                        ''.join(f'eu|{code}\n' for code in clients.values()))
    for name, code in clients.items():
        (folder / name).mkdir()
        (folder / name / '.flavor.info').write_text(f'Product Flavor!STRING:0\n{code}\n')
        (folder / name / 'Wow.exe').write_bytes(b'')
    return folder


def import_states(window, shot, application) -> None:
    """Importing Classic Era from installations found in a throwaway home, never the user's own folders."""
    from scuffle import imports
    home = Path(_home)
    fake_battle_net(home / 'Games/battlenet/drive_c/Program Files (x86)/World of Warcraft',
                    {'_retail_': 'wow', '_classic_era_': 'wow_classic_era', '_ptr_': 'wowt'})
    fake_battle_net(home / 'run/media/Games SSD/World of Warcraft', {'_classic_era_': 'wow_classic_era'})
    places = [imports.Place(home / 'Games', 'Games folder', 3), imports.Place(home / 'run/media', 'Mounted drive', 3)]
    original = imports.default_places

    def settle(seconds=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.02)

    menu = window.client_page.more.menu()
    window.refresh()
    menu.popup(window.client_page.more.mapToGlobal(window.client_page.more.rect().bottomLeft()))
    application.processEvents()
    menu.grab().save(str(window.screens_output / '30-import-menu.png'))
    menu.close()
    imports.default_places = lambda: []
    window.open_import()
    window._import_searching = True
    window.import_page.set_status('Looking in Wine prefixes, Steam, Lutris, ~/Games and mounted drives…')
    window._import_search += 1          # the empty search's answer must not end this state
    shot('30a-import-searching')
    imports.default_places = lambda: places
    window.open_import()
    settle()
    shot('30b-import-found')
    window.import_page.boxes['wowt'].setChecked(True)
    shot('30c-import-two-clients')
    window.import_page.copy.setChecked(False)
    shot('30d-import-without-settings')
    imports.default_places = lambda: []
    window.open_import()
    settle()
    shot('30e-import-nothing-found')
    window.choose_import_folder()
    settle(1.5)
    shot('30f-import-folder-picker')
    window.on_import_folder_chosen(str(home))
    settle()
    shot('30g-import-chosen-folder-empty')
    window.on_import_searched(window._import_search, False)
    window._import_searching = True
    window.on_import_searched(window._import_search, False)
    shot('30h-import-search-stopped')
    imports.default_places = original
    window.close_import()
    if window.downloads:
        job = Job('wow_classic_era', 'install', status=RUNNING, stage='seeding', done_bytes=9_800_000_000,
                  total_bytes=24_600_000_000, speed=0, seed_from=str(home / 'run/media/Games SSD/World of Warcraft'))
        window.downloads.jobs.append(job)
        window.downloads._current = job
        window.client_list.select('wow_classic_era')
        shot('30i-importing')
        job.stage, job.total_bytes = 'copying_settings', 0
        shot('30j-copying-settings')
        window.downloads.jobs.clear()
        window.downloads._current = None
        job.status, job.result = 'done', {'settings': {'copied': 412, 'kept': 0, 'skipped': 0, 'failed': 0}}
        window.on_job(job)
        shot('30k-imported')
        window.clear_notice('wow_classic_era')


def fake_installed(window, code: str) -> None:
    """Show a client as installed, as the tests do, when this machine has none. Nothing is written."""
    import tempfile
    if not window.library:
        window.settings.root = tempfile.mkdtemp(prefix='scuffle-screens-')
        window.open_library()
    state = window.library.state
    window.library.state = lambda c: ({'version': '12.0.5.66741', 'region': 'eu', 'locale': 'enUS',
                                       'folder': '_retail_'} if c == code else state(c))


def tray_menu(window, application, output: Path) -> None:
    """The tray icon's menu while a client downloads, with clients to play and one running."""
    from PySide6.QtCore import QPoint
    if not window.downloads:
        return
    job = Job('wow_classic_era', 'install', status=RUNNING, stage='downloading',
              done_bytes=12_400_000_000, total_bytes=31_800_000_000, speed=48_000_000)
    window.downloads.jobs[:] = [job, Job('wowf', 'install', status=PAUSED)]
    window.downloads._current = job
    installed = window.library.installed
    window.library.installed = lambda: ['wow', 'wow_classic']
    state = window.library.state
    window.library.state = lambda c: ({'version': '5.5.1.63538', 'region': 'eu', 'locale': 'enUS',
                                       'folder': '_classic_'} if c == 'wow_classic' else state(c))
    window.phases['wow_classic'] = 'Running'
    window.refresh()
    menu = window.tray.menu
    menu.popup(window.mapToGlobal(QPoint(40, 40)))
    for _ in range(3):
        application.processEvents()
    menu.grab().save(str(output / '23-tray-menu.png'))
    menu.close()
    window.phases.clear()
    window.library.installed, window.library.state = installed, state
    window.downloads.jobs.clear()
    window.downloads._current = None


def move_states(window, shot, application) -> None:
    """Moving the installation folder: the button, the picker, the confirmation and the progress."""
    window.library.create()                     # the throwaway folder becomes an installation to move
    window.open_settings()
    shot('14-settings-move')
    window.phases['wow'] = 'Running'
    window.open_settings()
    shot('14b-settings-move-refused', lambda: window.settings_page.move_button.setFocus())
    window.phases.clear()
    window.open_move_picker()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    shot('14c-move-picker')
    target = str(Path(_home) / 'second-drive' / 'Scuffle')
    page = window.move_page
    source = str(window.library.root)
    page.load(source, target)
    window.stack.setCurrentWidget(page)
    shot('14d-move-checking')
    page.show_preview({'destination': target, 'same_drive': False, 'size': 312_400_000_000,
                       'free': 1_840_000_000_000, 'problem': ''})
    shot('14e-move-other-drive')
    page.show_preview({'destination': target, 'same_drive': True, 'size': 312_400_000_000,
                       'free': 0, 'problem': ''})
    shot('14f-move-same-drive')
    page.show_preview({'destination': target, 'same_drive': False, 'size': 312_400_000_000,
                       'free': 120_000_000_000, 'problem': 'The new drive has 120.0 GB free, but the '
                       'installation needs 313.4 GB there.'})
    shot('14g-move-no-space')
    task = MoveTask(source, target, status=RUNNING, stage='copying', done_bytes=141_000_000_000,
                    total_bytes=312_400_000_000, speed=182_000_000)
    window.downloads.moving = task
    window.open_settings()
    shot('14h-moving-copying')
    window.show_client_page()
    shot('14i-moving-client')
    task.stage, task.switched = 'removing_old', True
    shot('14j-moving-removing-old')
    window.downloads.moving = None
    window.show_client_page()


if __name__ == '__main__':
    main(Path(sys.argv[1] if len(sys.argv) > 1 else 'screens'))
