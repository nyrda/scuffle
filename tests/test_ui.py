"""Check the sign-in page's clipboard handling with a real Qt clipboard."""
import os
import sys
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if sys.platform == 'win32':
    # Offscreen Qt on Windows finds no fonts by itself, and its wider fallback breaks the layout checks.
    os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))
QtWidgets = pytest.importorskip('PySide6.QtWidgets')
QApplication = QtWidgets.QApplication

from scuffle.ui import pages  # noqa: E402


@pytest.fixture(scope='module')
def qt():
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield application
    # Delete every widget while the application still exists. Python's shutdown
    # order would otherwise destroy the application first and crash.
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()


def test_copied_redirect_signs_in_once_and_leaves_no_ticket_on_the_clipboard(qt, monkeypatch):
    monkeypatch.setattr(pages, 'open_browser', lambda url: None)
    page = pages.SignInPage()
    results = []
    page.finished.connect(results.append)
    clipboard = qt.clipboard()
    clipboard.setText('unrelated text')
    page.start('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    redirect = 'http://localhost:0/?ST=EU-0123abcd-314159265&flowTrackingId='
    clipboard.setText(redirect)
    for _ in range(5):
        qt.processEvents()
    assert results == [b'EU-0123abcd-314159265']
    assert 'ST=' not in clipboard.text()
    # On Wayland a late change notification can still report the old address.
    # Handling it again used to recurse until Python's recursion limit.
    clipboard.setText(redirect)
    for _ in range(5):
        qt.processEvents()
    assert results == [b'EU-0123abcd-314159265']


def test_clipboard_text_from_before_the_sign_in_is_ignored(qt, monkeypatch):
    monkeypatch.setattr(pages, 'open_browser', lambda url: None)
    qt.clipboard().setText('http://localhost:0/?ST=EU-old-1&flowTrackingId=')
    page = pages.SignInPage()
    results = []
    page.finished.connect(results.append)
    page.start('https://eu.account.battle.net/login/?externalChallenge=login&app=wow')
    page.check_clipboard()
    qt.processEvents()
    assert results == []


def test_a_worker_that_finishes_after_the_window_is_gone_does_not_raise(qt):
    import shiboken6
    from scuffle.ui import app as ui
    bus = ui.Bus()
    shiboken6.delete(bus)           # as when the window closed while the running-games scan ran
    ui.post(bus.running_scanned, None, None)


def test_mode_buttons_are_replaced_not_added_when_switching_clients(qt):
    from scuffle.ui.widgets import Segmented
    modes = Segmented()
    modes.set_options([('era', 'Classic Era'), ('hardcore', 'Hardcore')], 'era')
    modes.set_options([], None)
    modes.set_options([('era', 'Classic Era'), ('sod', 'Season of Discovery')], 'sod')
    # Checked before the event loop runs, which is when the window paints a refresh.
    laid_out = [modes.layout_.itemAt(i).widget() for i in range(modes.layout_.count())]
    assert [button.text() for button in laid_out] == ['Classic Era', 'Season of Discovery']
    assert modes.value() == 'sod'


@pytest.fixture
def window(qt, tmp_path, monkeypatch):
    """A window with empty settings, so the user's real configuration is never read or written."""
    from scuffle import discovery, settings as settings_module
    from scuffle.ui import app as ui

    def offline(url, *args, **kwargs):
        raise OSError('Tests never reach the version service')
    monkeypatch.setattr(discovery, 'fetch', offline)
    for name in ['config_dir', 'data_dir', 'cache_dir']:
        monkeypatch.setattr(settings_module, name, lambda name=name: tmp_path / name)
        monkeypatch.setattr(ui, name, lambda name=name: tmp_path / name, raising=False)
    window = ui.MainWindow()
    window.show()
    qt.processEvents()
    yield window
    window.close()


def test_clicking_the_selected_client_leaves_the_settings(qt, window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window.open_settings()
    assert window.stack.currentWidget() is window.settings_page
    item = window.client_list.currentItem()
    QTest.mouseClick(window.client_list.viewport(), Qt.LeftButton,
                     pos=window.client_list.visualItemRect(item).center())
    assert window.stack.currentWidget() is window.client_page


def test_queue_controls_survive_progress_updates(qt):
    from scuffle.downloads import RUNNING, Job
    queue = pages.QueuePage()
    job = Job('wow', 'install', status=RUNNING, stage='downloading', done_bytes=1, total_bytes=100)
    queue.show_jobs([job], {'wow': 'World of Warcraft'})
    pause = queue.cards['wow']['toggle']
    job.done_bytes = 2
    queue.show_jobs([job], {'wow': 'World of Warcraft'})
    # The same button, so keyboard focus on it is not lost every percent.
    assert queue.cards['wow']['toggle'] is pause
    assert '2%' in queue.cards['wow']['detail'].text()


def test_narrow_download_bar_keeps_its_progress_and_shortens_the_details_whole(qt):
    from scuffle.downloads import RUNNING, Job
    from scuffle.ui.widgets import DownloadBar
    bar = DownloadBar()
    job = Job('wow_classic', 'install', status=RUNNING, stage='downloading', done_bytes=46_200_000_000,
              total_bytes=75_800_000_000, speed=22_600_000)
    bar.show_job(job, 'Mists of Pandaria Classic', waiting=1)
    bar.resize(480, 64)
    bar.show()
    qt.processEvents()
    assert bar.progress.isVisible() and bar.progress.value() == 609
    shown = bar.detail.text()
    # A whole shorter line, not a cut one, and the full line stays in the tooltip.
    assert shown.startswith('61%') and '…' not in shown and shown != bar.detail.full
    assert bar.detail.toolTip().endswith('of 75.8 GB')


def test_the_folder_cannot_change_under_queued_downloads(qt, window, tmp_path):
    from scuffle.downloads import DONE, QUEUED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    job = Job('wow', 'install', status=QUEUED)
    window.downloads.jobs.append(job)
    window.open_settings()
    page = window.settings_page
    assert not page.browse.isEnabled() and 'downloads' in page.free.text()
    job.status = DONE
    window.on_job(job)
    assert page.browse.isEnabled() and 'free' in page.free.text()


@pytest.fixture
def styled(qt, monkeypatch, tmp_path):
    """The real style and a Proton build with a long name, set before the window is built."""
    from scuffle.ui import app as ui
    monkeypatch.setattr(ui, 'cache_dir', lambda: tmp_path / 'style')
    ui.prepare(qt)
    from scuffle import launch
    from scuffle.runtime.proton import Proton
    from scuffle.ui import launch_options
    build = Proton('GE-Proton10-25 with a very long name from a custom folder', tmp_path / 'proton')
    build.path.mkdir()
    # Both the list and the build a launch would pick, so nothing depends on this machine's builds.
    monkeypatch.setattr(launch_options, 'find_protons', lambda: [build])
    monkeypatch.setattr(launch, 'find_protons', lambda: [build])


def test_settings_never_scroll_sideways_at_the_minimum_size(qt, styled, window):
    import json
    from pathlib import Path
    from PySide6.QtWidgets import QScrollArea
    from scuffle import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    window.on_catalog(window.settings.region, snapshot)
    window.settings.show_other_clients = True
    window.open_settings()
    window.settings_page.load(window.settings, [c.code for c in catalog.clients() if c.test],
                              {'wowlivetest': 'Install failed'})
    window.resize(960, 650)
    for section, _ in window.settings_page.SECTIONS:
        window.settings_page.show_section(section)
        for _ in range(3):
            qt.processEvents()
        assert window.settings_page.findChild(QScrollArea).horizontalScrollBar().maximum() == 0, section
    catalog.apply(None)


def test_the_folder_picker_reveals_the_current_folder_once_it_has_loaded(qt, tmp_path):
    from pathlib import Path
    import time
    for name in ['alpha', 'beta', 'gamma', *(f'filler {n:02}' for n in range(40)), 'zeta']:
        (tmp_path / name).mkdir()
    target = tmp_path / 'zeta'
    page = pages.FolderPage()
    page.resize(700, 400)
    page.show()
    page.start(str(target))

    def revealed():
        rect = page.tree.visualRect(page.tree.currentIndex())
        return (Path(page.model.filePath(page.tree.currentIndex())) == target
                and 0 <= rect.top() and rect.bottom() <= page.tree.viewport().height())
    # Folders load in the background, so how soon this happens depends on the machine.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not revealed():
        qt.processEvents()
        time.sleep(0.01)
    rect = page.tree.visualRect(page.tree.currentIndex())
    assert Path(page.model.filePath(page.tree.currentIndex())) == target
    assert 0 <= rect.top() and rect.bottom() <= page.tree.viewport().height()


def test_a_queued_client_offers_nothing_to_pause(qt, window, tmp_path):
    from scuffle.downloads import QUEUED, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    running = Job('wow', 'install', status=RUNNING, done_bytes=1, total_bytes=10)
    window.downloads.jobs.extend([running, Job('wowf', 'install', status=QUEUED)])
    window.downloads._current = running
    window.client_list.select('wowf')
    window.refresh()
    assert window.client_page.primary.text() == 'Queued'
    assert not window.client_page.primary.isEnabled()
    assert window.client_page.cancel.isVisible()
    window.downloads._current = None       # nothing really runs, so closing need not pause it


def test_the_sidebar_cannot_start_a_second_sign_in_during_one(qt, window, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt
    monkeypatch.setattr(pages, 'open_browser', lambda url: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    monkeypatch.setattr(window.library, 'installed', lambda: ['wow'])
    before = window.client_list.currentItem()
    window.on_sign_in_requested('https://example.invalid/login')
    # Signing out, or starting another sign-in, would race for the single-use ticket.
    assert not window.account_button.isEnabled()
    assert not window.settings_button.isEnabled()
    assert window.client_list.viewport().testAttribute(Qt.WA_TransparentForMouseEvents)
    window.on_sign_in_finished(None)
    assert window.account_button.isEnabled() and window.settings_button.isEnabled()
    assert window.client_list.currentItem() is before


def test_dismissing_a_failure_clears_it_from_the_list(qt, window, tmp_path):
    from scuffle.downloads import FAILED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    failed = Job('wowf', 'install', status=FAILED, error='The servers did not answer.')
    window.downloads.jobs.append(failed)
    window.client_list.select('wowf')
    window.on_job(failed)
    assert window.client_list.rows['wowf'].status.text() == 'Install failed'
    window.client_page.banner.close_button.click()
    assert window.downloads.job('wowf') is None
    assert window.client_list.rows['wowf'].status.text() == 'Not installed'
    assert window.client_page.primary.text() == 'Install'


def test_first_run_settings_save_before_a_folder_is_chosen(qt, window):
    from scuffle.settings import Settings
    window.open_settings()
    page = window.settings_page
    page.region.setCurrentIndex(page.region.findData('us'))
    page.choices['wowt']['box'].setChecked(True)
    saved = Settings.load()
    assert saved.root == '' and saved.region == 'us' and saved.extra_clients == ['wowt']


def test_escape_closes_settings_right_after_opening_them_from_the_sidebar(qt, window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window.activateWindow()
    window.settings_button.click()
    assert window.stack.currentWidget() is window.settings_page
    QTest.keyClick(QApplication.focusWidget() or window, Qt.Key_Escape)
    assert window.stack.currentWidget() is window.client_page


def test_a_failed_download_stays_on_the_downloads_page_with_a_retry(qt):
    from scuffle.downloads import FAILED, Job
    queue = pages.QueuePage()
    queue.show_jobs([Job('wow', 'install', status=FAILED, error='The servers did not answer.')],
                    {'wow': 'World of Warcraft'})
    card = queue.cards['wow']
    assert 'The servers did not answer.' in card['detail'].text()
    assert card['toggle'].text() == 'Try again' and card['cancel'].text() == 'Remove'


def test_progress_after_the_transfer_names_the_stage_instead_of_a_time(qt):
    from scuffle.downloads import RUNNING, Job
    from scuffle.ui.widgets import progress_variants
    job = Job('wow', 'install', status=RUNNING, done_bytes=100, total_bytes=100, speed=40, stage='materializing')
    assert progress_variants(job) == ['Writing game files…']
    job.stage, job.total_bytes = 'seeding_cdn_index', 0
    assert progress_variants(job) == ['Fetching download indexes…']
    # Known sizes stay out of a long step before the transfer, which counts what it does instead.
    job.stage, job.total_bytes, job.done_bytes, job.step = 'cdn_indexes', 91_200_000_000, 90_800_000_000, (1200, 1411)
    assert progress_variants(job) == ['Fetching download indexes · 1,200 of 1,411', 'Fetching download indexes…']


def test_files_cannot_be_checked_while_the_game_runs(qt, window, tmp_path, monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    monkeypatch.setattr(window.library, 'state', lambda code: {'version': '1.0.0.1', 'region': 'eu',
                                                                'locale': 'enUS'} if code == 'wowf' else None)
    window.client_list.select('wowf')
    window.phases['wowf'] = 'Running'
    window.refresh()
    assert not window.client_page.verify_action.isEnabled()
    assert window.status('wowf') == ('Running', 'active')


def test_the_download_bar_cannot_switch_clients_during_a_sign_in(qt, window, monkeypatch):
    monkeypatch.setattr(pages, 'open_browser', lambda url: None)
    before = window.selected()
    window.on_sign_in_requested('https://example.invalid/login')
    window.open_client('wow_classic' if before != 'wow_classic' else 'wow')
    assert window.selected() == before
    window.on_sign_in_finished(None)


def test_a_downloading_test_client_stays_listed_after_unchecking_it(qt, window, tmp_path):
    from scuffle.downloads import RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.settings.extra_clients = []
    job = Job('wowt', 'install', status=RUNNING, done_bytes=1, total_bytes=10)
    window.downloads.jobs.append(job)
    window.on_job(job)
    assert 'wowt' in window.client_list.rows
    window.open_settings()
    assert not window.settings_page.choices['wowt']['box'].isEnabled()
    window.downloads.jobs.clear()


def test_a_long_failure_never_pushes_the_queue_buttons_out(qt):
    from PySide6.QtWidgets import QScrollArea
    from scuffle.downloads import FAILED, Job
    queue = pages.QueuePage()
    queue.resize(680, 500)
    queue.show()
    queue.show_jobs([Job('wow', 'install', status=FAILED, error='The servers did not answer. ' * 6)],
                    {'wow': 'World of Warcraft'})
    for _ in range(3):
        qt.processEvents()
    assert queue.findChild(QScrollArea).horizontalScrollBar().maximum() == 0


def test_a_path_being_typed_survives_the_folder_tree_loading(qt, tmp_path):
    from PySide6.QtTest import QTest
    (tmp_path / 'games').mkdir()
    page = pages.FolderPage()
    page.show()
    page.start(str(tmp_path / 'games'))
    page.path.clear()
    QTest.keyClicks(page.path, str(tmp_path / 'new folder'))
    (tmp_path / 'sibling').mkdir()             # the model refreshes and loads more rows
    page._reveal()                              # what a late model signal would trigger
    assert page.path.text() == str(tmp_path / 'new folder')


def test_dismissing_found_damage_keeps_the_repair(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import FAILED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    monkeypatch.setattr(window.library, 'state', lambda code: {'version': '1.0.0.1', 'region': 'eu',
                                                                'locale': 'enUS'} if code == 'wowf' else None)
    damaged = Job('wowf', 'verify', status=FAILED, error='2 damaged or missing files found.')
    window.downloads.jobs.append(damaged)
    window.client_list.select('wowf')
    window.on_job(damaged)
    window.client_page.banner.close_button.click()
    assert window.status('wowf') == ('Needs repair', 'danger')
    assert window.client_page.primary.text() == 'Repair'


def test_a_cancelled_test_client_download_unlocks_its_checkbox(qt, window, tmp_path):
    from scuffle.downloads import QUEUED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.settings.extra_clients = []
    job = Job('wowt', 'install', status=QUEUED)
    window.downloads.jobs.append(job)
    window.open_settings()
    box = window.settings_page.choices['wowt']['box']
    assert not box.isEnabled() and 'Queued' in box.toolTip()
    window.downloads.jobs.clear()
    window.on_job(job)
    assert box.isEnabled() and not box.isChecked()


def test_an_update_notice_says_when_it_waits_and_goes_when_done(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DONE, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    running = Job('wowf', 'install', status=RUNNING)
    window.downloads.jobs.append(running)
    window.downloads._current = running
    # Queued without waking the worker, so nothing really runs.
    monkeypatch.setattr(window.downloads, 'enqueue',
                        lambda code, kind: window.downloads.jobs.append(Job(code, kind)) or window.downloads.jobs[-1])
    window.client_list.select('wow')
    window.on_update_needed('wow')
    assert 'after the current download' in window.messages['wow'][0]
    update = window.downloads.job('wow')
    update.status = DONE
    window.on_job(update)
    assert 'wow' not in window.messages
    window.downloads._current = None
    window.downloads.jobs.clear()


def test_queue_focus_stays_on_a_card_when_another_job_finishes(qt):
    from scuffle.downloads import QUEUED, RUNNING, Job
    queue = pages.QueuePage()
    queue.show()
    queue.activateWindow()
    first, second = Job('wow', 'install', status=RUNNING), Job('wowf', 'install', status=QUEUED)
    labels = {'wow': 'World of Warcraft', 'wowf': 'Forever'}
    queue.show_jobs([first, second], labels)
    cancel = queue.cards['wowf']['cancel']
    cancel.setFocus()
    second.status = RUNNING
    queue.show_jobs([second], labels)
    # The same button, so focus on it is not dropped onto the sidebar.
    assert queue.cards['wowf']['cancel'] is cancel


def test_a_locked_test_client_keeps_its_own_choice_through_other_changes(qt, window, tmp_path):
    from scuffle.downloads import FAILED, PAUSED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.settings.extra_clients = ['wow_classic_era_ptr']
    paused = Job('wow_classic_era_ptr', 'install', status=PAUSED)
    window.downloads.jobs.append(paused)
    window.open_settings()
    page = window.settings_page
    page.limit.setValue(5)
    assert window.settings.extra_clients == ['wow_classic_era_ptr']
    assert page.choices['wow_classic_era_ptr']['note'].text() == 'Paused'
    paused.status = FAILED
    window.on_job(paused)
    assert page.choices['wow_classic_era_ptr']['note'].text() == 'Install failed'
    window.downloads.jobs.clear()
    window.on_job(paused)
    assert page.choices['wow_classic_era_ptr']['box'].isChecked()
    assert 'wow_classic_era_ptr' in window.client_list.rows


def test_home_in_the_client_list_goes_to_the_first_client(qt, window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window.client_list.select('wowf')
    window.client_list.setFocus()
    QTest.keyClick(window.client_list, Qt.Key_Home)
    assert window.selected() == 'wow'
    QTest.keyClick(window.client_list, Qt.Key_Down)
    assert window.selected() != 'wow' and window.client_list.currentItem().data(Qt.UserRole)


def test_a_queued_client_names_the_job_right_before_it(qt, window, tmp_path):
    from scuffle.downloads import QUEUED, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    running = Job('wow_classic', 'install', status=RUNNING)
    window.downloads.jobs.extend([running, Job('wow_anniversary', 'install', status=QUEUED),
                                  Job('wow_classic_era', 'install', status=QUEUED)])
    window.downloads._current = running
    window.client_list.select('wow_classic_era')
    window.refresh()
    assert window.client_page.phase.full == 'Starts after Burning Crusade Anniversary'
    window.downloads._current = None
    window.downloads.jobs.clear()


def test_a_queued_client_does_not_name_a_paused_job_as_ahead(qt, window, tmp_path):
    from scuffle.downloads import PAUSED, QUEUED, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    running = Job('wow', 'update', status=RUNNING)
    window.downloads.jobs.extend([running, Job('wow_classic', 'install', status=PAUSED),
                                  Job('wow_classic_era', 'install', status=QUEUED)])
    window.downloads._current = running
    window.client_list.select('wow_classic_era')
    window.refresh()
    assert window.client_page.phase.full == 'Starts after World of Warcraft'
    window.downloads._current = None
    window.downloads.jobs.clear()


def test_settings_tab_goes_from_the_sections_into_the_one_shown(qt, window):
    from PySide6.QtWidgets import QComboBox
    page = window.settings_page
    window.open_settings()
    chain = page.focus_chain()
    sections = page.sections.group.buttons()
    # Every section is in the chain, and only the chosen one takes Tab. Arrow keys move between them.
    assert chain[:2 + len(sections)] == [page.close_button, *sections, page.browse] and page.limit not in chain
    page.show_section('launch')
    extra = QComboBox()
    page.add_launch_widget(extra)       # how launch options built elsewhere join the page
    qt.processEvents()
    chain = page.focus_chain()
    assert chain[-1] is extra and page.browse not in chain
    if page.proton is not None:
        assert chain.index(page.proton) == 1 + len(sections)
    assert extra.isVisibleTo(page) and page.section_widgets['launch'].isAncestorOf(extra)


def test_correcting_the_folder_path_clears_the_old_complaint(qt, tmp_path):
    page = pages.FolderPage()
    page.path.setText('relative/path')
    page.choose()
    assert page.problem.isVisibleTo(page)
    page.path.setText(str(tmp_path))
    page.go_to_typed()
    assert not page.problem.isVisibleTo(page)


def test_ctrl_c_in_the_terminal_closes_the_window(qt, window):
    import signal
    from scuffle.ui import app
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        app.close_on_signals(window)
        signal.raise_signal(signal.SIGINT)      # os.kill on Windows terminates the process instead
        for _ in range(5):
            qt.processEvents()
        assert not window.isVisible()
        assert signal.getsignal(signal.SIGINT) == signal.SIG_DFL     # a second Ctrl+C quits at once
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def installed(window, monkeypatch, codes):
    """Pretend these clients are installed, without a real installation or update check."""
    monkeypatch.setattr(window, 'check_updates', lambda: None)
    monkeypatch.setattr(window.library, 'installed', lambda: list(codes))
    monkeypatch.setattr(window.library, 'state', lambda code: {
        'version': '12.1.5.70575', 'region': 'eu', 'locale': 'enUS', 'folder': '_retail_'} if code in codes else None)


def test_an_installed_client_can_be_hidden_from_its_menu_and_brought_back(qt, window, tmp_path, monkeypatch):
    from scuffle.settings import Settings
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.refresh()
    page = window.client_page
    assert page.more.isVisibleTo(page) and page.hide_action.isEnabled()
    page.hide_action.trigger()
    assert 'wow' not in window.client_list.rows and window.selected() != 'wow'
    assert Settings.load().hidden_clients == ['wow']
    assert 'hidden' in page.banner.text.text()
    page.banner.action.click()          # Undo
    assert 'wow' in window.client_list.rows and window.selected() == 'wow'
    assert Settings.load().hidden_clients == []


def test_a_client_with_a_download_cannot_be_hidden(qt, window, tmp_path):
    from scuffle.downloads import FAILED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    failed = Job('wowf', 'install', status=FAILED, error='The servers did not answer.')
    window.downloads.jobs.append(failed)
    window.on_job(failed)
    window.client_list.select('wowf')
    window.refresh()
    assert not window.client_page.hide_action.isEnabled()
    window.hide_client()
    assert window.settings.hidden_clients == []
    # Hidden in the settings, it still stays until its download is gone.
    window.settings.hidden_clients = ['wowf']
    window.populate()
    assert 'wowf' in window.client_list.rows
    window.downloads.jobs.clear()
    window.on_job(failed)
    assert 'wowf' not in window.client_list.rows


def test_with_every_client_hidden_the_list_points_to_the_settings(qt, window):
    from scuffle import catalog
    window.settings.hidden_clients = [c.code for c in catalog.clients()]
    window.populate()
    window.refresh()
    assert window.client_list.rows == {} and window.client_list.empty.isVisibleTo(window)
    assert window.client_page.empty.isVisibleTo(window.client_page)
    window.client_list.empty_button.click()
    assert window.stack.currentWidget() is window.settings_page and window.settings_page.section == 'clients'
    window.settings_page.choices['wow']['box'].setChecked(True)
    assert list(window.client_list.rows) == ['wow'] and not window.client_list.empty.isVisibleTo(window)


def test_other_public_clients_stay_collapsed_until_they_are_shown(qt, window):
    import json
    from pathlib import Path
    from scuffle import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    window.on_catalog(window.settings.region, snapshot)
    window.open_settings('clients')
    page = window.settings_page
    other = page.choices['wowlivetest']
    assert other['category'] == catalog.OTHER and not other['box'].isVisibleTo(page)
    assert page.choices['wow_beta']['note'].text() == 'Older than live'
    assert page.choices['wowlivetest']['note'].text() == 'Old build'
    other['box'].setChecked(True)       # a choice made earlier, kept while the box is off
    assert 'wowlivetest' not in window.client_list.rows
    page.show_other.setChecked(True)
    assert other['box'].isVisibleTo(page) and 'wowlivetest' in window.client_list.rows
    assert window.settings.show_other_clients and window.settings.extra_clients == ['wowlivetest']
    assert other['box'] in page.focus_chain()
    page.show_other.setChecked(False)
    assert 'wowlivetest' not in window.client_list.rows and window.settings.extra_clients == ['wowlivetest']
    catalog.apply(None)


def test_without_the_version_service_the_other_clients_say_so(qt, window):
    window.on_catalog(window.settings.region, None)
    window.open_settings('clients')
    page = window.settings_page
    page.show_other.setChecked(True)
    assert 'could not be reached' in page.other_status.text() and page.other_status.isVisibleTo(page)
    assert not any(c['category'] == 'other' for c in page.choices.values())


def test_unticking_a_live_or_installed_client_hides_it_and_a_download_locks_it_on(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import QUEUED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wowt'])
    window.populate()
    window.open_settings('clients')
    page = window.settings_page
    assert page.choices['wowt']['box'].isChecked() and page.choices['wowt']['box'].isEnabled()
    assert page.choices['wowt']['note'].text() == 'Installed'
    page.choices['wowt']['box'].setChecked(False)
    page.choices['wow_classic']['box'].setChecked(False)
    assert window.settings.hidden_clients == ['wow_classic', 'wowt']
    assert 'wowt' not in window.client_list.rows and 'wow_classic' not in window.client_list.rows
    job = Job('wow_classic', 'install', status=QUEUED)
    window.downloads.jobs.append(job)
    window.on_job(job)
    box = page.choices['wow_classic']['box']
    assert box.isChecked() and not box.isEnabled() and 'wow_classic' in window.client_list.rows
    page.limit.setValue(5)      # another change keeps the user's own choice
    assert 'wow_classic' in window.settings.hidden_clients
    window.downloads.jobs.clear()


def test_the_window_opens_before_the_version_service_answers_and_uses_the_answer(qt, tmp_path, monkeypatch):
    import json
    import threading
    import time
    from pathlib import Path
    from scuffle import catalog, discovery, settings as settings_module
    from scuffle.ui import app as ui
    for name in ['config_dir', 'data_dir', 'cache_dir']:
        monkeypatch.setattr(settings_module, name, lambda name=name: tmp_path / name)
        monkeypatch.setattr(ui, name, lambda name=name: tmp_path / name, raising=False)
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    answer = threading.Event()

    def slow(region, cache, previous=None):
        if not answer.wait(timeout=3):
            raise OSError('nobody answered')
        return snapshot | {'region': region}
    monkeypatch.setattr(discovery, 'refresh', slow)
    started = time.monotonic()
    window = ui.MainWindow()
    try:
        assert time.monotonic() - started < 2 and window.catalog_state == 'checking'
        assert 'wowlivetest' not in window.settings_page.choices
        answer.set()
        deadline = time.monotonic() + 3
        while window.catalog_state == 'checking' and time.monotonic() < deadline:
            qt.processEvents()
            time.sleep(0.01)
        assert 'wowlivetest' in window.settings_page.choices
        assert discovery.load(tmp_path / 'cache_dir', window.settings.region) is not None
    finally:
        # Closed and deleted while the folders are still the test's own, since closing saves.
        from PySide6.QtCore import QCoreApplication, QEvent
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        catalog.apply(None)


def test_choosing_another_region_switches_to_that_regions_clients(qt, window, tmp_path):
    import json
    from pathlib import Path
    from scuffle import catalog, discovery
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    discovery.save(tmp_path / 'cache_dir', snapshot)
    window.open_settings()
    page = window.settings_page
    assert window.settings.region == 'eu' and 'wowlivetest' not in page.choices
    page.region.setCurrentIndex(page.region.findData('us'))
    assert 'wowlivetest' in page.choices and catalog.release('wow').version == '12.1.5.70575'
    catalog.apply(None)


def test_a_finished_job_no_longer_keeps_its_client_listed_or_locked(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DONE, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    done = Job('wow', 'verify', status=DONE)
    window.downloads.jobs.append(done)        # the manager keeps a finished job until the next one
    window.on_job(done)
    window.client_list.select('wow')
    window.refresh()
    assert window.client_page.hide_action.isEnabled()
    window.open_settings('clients')
    choice = window.settings_page.choices['wow']
    assert choice['box'].isEnabled() and choice['note'].text() == 'Installed'
    window.show_client_page()
    window.hide_client()
    assert window.settings.hidden_clients == ['wow'] and 'wow' not in window.client_list.rows
    window.downloads.jobs.clear()


def test_a_hidden_client_the_catalog_no_longer_offers_can_be_shown_again(qt, window, tmp_path, monkeypatch):
    from scuffle import catalog
    window.settings.root = str(tmp_path / 'games')
    window.settings.show_other_clients = True
    window.open_library()
    installed(window, monkeypatch, ['wowe1'])     # installed earlier, not found by discovery now
    window.populate()
    assert 'wowe1' in window.client_list.rows and 'wowe1' not in [c.code for c in catalog.clients()]
    window.client_list.select('wowe1')
    window.refresh()
    window.hide_client()
    assert 'wowe1' not in window.client_list.rows
    window.open_settings('clients')
    box = window.settings_page.choices['wowe1']['box']
    assert box.isVisibleTo(window.settings_page) and not box.isChecked()
    box.setChecked(True)
    assert window.settings.hidden_clients == [] and 'wowe1' in window.client_list.rows


def test_open_settings_rows_follow_a_new_build_with_the_same_clients(qt, window):
    import json
    from pathlib import Path
    from scuffle import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    window.on_catalog(window.settings.region, snapshot)
    window.open_settings('clients')
    row = window.settings_page.choices['wow_beta']
    assert row['version'].text() == '12.0.1.66220' and row['note'].text() == 'Older than live'
    snapshot['products']['wow_beta']['version'] = '12.1.9.71000'        # a new beta, the same products
    window.on_catalog(window.settings.region, snapshot)
    assert row is window.settings_page.choices['wow_beta']
    assert row['version'].text() == '12.1.9.71000' and row['note'].text() == ''
    catalog.apply(None)


def test_a_short_fact_fills_the_free_cell_instead_of_a_row_of_its_own(qt):
    from scuffle.ui.widgets import FactsCard
    card = FactsCard()
    card.set_facts([('Version', '1.15.9.70576'), ('Region', 'Europe'), ('Language', 'English (US)'),
                    ('Download size', 'Not known yet')])
    position = {key.text(): card.grid.getItemPosition(card.grid.indexOf(key))[:2] for key, _ in card.cells}
    assert position == {'Version': (0, 0), 'Region': (0, 2), 'Language': (1, 0), 'Download size': (1, 2)}
    # A path keeps its full row.
    card.set_facts([('Version', '1.15.9.70576'), ('Region', 'Europe'), ('Language', 'English (US)'),
                    ('Folder', '/games/_classic_era_')])
    key, value = card.cells[-1]
    assert key.text() == 'Folder' and card.grid.getItemPosition(card.grid.indexOf(value)) == (2, 1, 1, 3)


def installed_wow(window, tmp_path, monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    monkeypatch.setattr(window.library, 'state', lambda code: {'version': '12.0.5.66741', 'region': 'eu',
                                                                'locale': 'enUS', 'folder': '_retail_'}
                        if code == 'wow' else None)
    window.client_list.select('wow')
    window.refresh()


def test_a_clients_launch_options_save_restore_and_show_on_its_page(qt, window, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from scuffle.launch_options import ClientOptions
    from scuffle.settings import Settings
    installed_wow(window, tmp_path, monkeypatch)
    window.client_page.launch_action.trigger()
    page = window.options_page
    assert window.stack.currentWidget() is page
    next(b for b in page.graphics.group.buttons() if b.text() == 'DirectX 11').click()
    page.switches['-console'].setChecked(True)
    page.arguments.setText('-config Other.wtf')
    if page.environment:
        page.environment.text.setPlainText('DXVK_HUD=fps')
    QTest.keyClick(page.close_button, Qt.Key_Escape)          # Esc returns at once and keeps the typing
    assert window.stack.currentWidget() is window.client_page
    expected = ClientOptions(graphics='-d3d11', switches=['-console'], arguments='-config Other.wtf',
                             environment={'DXVK_HUD': 'fps'} if page.environment else {})
    assert Settings.load().client_launch == {'wow': expected}
    window.refresh()
    assert ('Launch options', '-d3d11 -console -config Other.wtf' + (' · 1 variable' if page.environment else '')) \
        in window.facts('wow')
    assert 'launch options' in window.client_page.primary.toolTip()
    # A reserved argument is pointed out and not saved. Reopening shows what was saved.
    window.open_launch_options()
    assert page.switches['-console'].isChecked() and page.graphics.value() == '-d3d11'
    page.arguments.setText('-config Other.wtf -uid wow')
    page.close_button.click()
    assert Settings.load().client_launch['wow'].arguments == '-config Other.wtf'
    # Reopening keeps the text being corrected, with its note, while the saved value stays.
    window.open_launch_options()
    assert page.arguments.text() == '-config Other.wtf -uid wow' and '-uid' in page.arguments_note.text()
    page.reset_button.click()
    assert Settings.load().client_launch == {}


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux launch settings')
def test_linux_launch_settings_save_valid_input_and_keep_it_through_mistakes(qt, window, programs_on_path):
    from scuffle.settings import Settings
    window.open_settings()
    launch = window.settings_page.launch
    launch.boxes['log'].setChecked(True)          # works with any build and machine
    launch.environment.text.setPlainText('DXVK_HUD=fps')
    launch.environment.flush()
    launch.wrapper.setText('gamescope -f --')
    launch.wrapper.editingFinished.emit()
    saved = Settings.load().linux_launch
    assert saved.log and saved.environment == {'DXVK_HUD': 'fps'} and saved.wrapper == 'gamescope -f --'
    assert not launch.wrapper_note.isVisibleTo(launch)
    # Mistakes are named under their field. Good lines save, a bad one waits until it is fixed.
    launch.environment.text.setPlainText('DXVK_HUD=full\nBAD NAME=1')
    launch.environment.flush()
    launch.wrapper.setText('missing-tool --')
    launch.wrapper.editingFinished.emit()
    assert 'Line 2' in launch.environment.problem.text() and launch.environment.problem.isVisibleTo(launch)
    assert 'missing-tool was not found' in launch.wrapper_note.text()
    saved = Settings.load().linux_launch
    assert saved.environment == {'DXVK_HUD': 'full'} and saved.wrapper == 'missing-tool --'
    launch.wrapper.setText('gamescope "unclosed')
    launch.wrapper.editingFinished.emit()
    assert Settings.load().linux_launch.wrapper == 'missing-tool --'


@pytest.fixture
def programs_on_path(tmp_path, monkeypatch):
    folder = tmp_path / 'bin'
    folder.mkdir()
    (folder / 'gamescope').write_text('#!/bin/sh\n')
    (folder / 'gamescope').chmod(0o755)
    monkeypatch.setenv('PATH', str(folder))
    return folder


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux launch settings')
def test_an_option_that_cannot_work_here_shows_off_and_keeps_its_choice(qt, styled, window, programs_on_path):
    from scuffle.launch_options import LinuxOptions
    from scuffle.settings import Settings
    window.settings.linux_launch = LinuxOptions(gamemode=True, wayland=True)
    window.open_settings()
    launch = window.settings_page.launch
    # No gamemoderun, and a Proton build without Wayland.
    for name, note in [('gamemode', 'gamemoderun was not found'), ('wayland', 'Not in this Proton build')]:
        box = launch.boxes[name]
        assert not box.isChecked() and not box.isEnabled() and note in launch.notes[name].text()
    launch.boxes['log'].setChecked(True)
    saved = Settings.load().linux_launch
    assert saved.log and saved.gamemode and saved.wayland
    # Once gamemoderun is installed, the saved choice shows again.
    (programs_on_path / 'gamemoderun').write_text('#!/bin/sh\n')
    (programs_on_path / 'gamemoderun').chmod(0o755)
    launch.update_notes()
    assert launch.boxes['gamemode'].isChecked() and launch.boxes['gamemode'].isEnabled()


def test_the_client_page_without_linux_saves_arguments_and_keeps_hidden_variables(qt):
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=False)
    saved = []
    page.changed.connect(lambda code, options: saved.append((code, options)))
    page.load(catalog.get('wow'), ClientOptions(switches=['-console'], environment={'DXVK_HUD': 'fps'}))
    assert page.environment is None and page.focus_chain()[-1] is page.arguments
    page.switches['-nosound'].setChecked(True)
    # The variables were set on Linux. This page cannot show them, and keeps them as they were.
    assert saved[-1] == ('wow', ClientOptions(switches=['-console', '-nosound'], environment={'DXVK_HUD': 'fps'}))
    page.reset_button.click()
    assert saved[-1] == ('wow', ClientOptions())


def test_extra_arguments_that_contradict_a_toggle_save_with_a_calm_note(qt):
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=False)
    saved = []
    page.changed.connect(lambda code, options: saved.append(options))
    page.load(catalog.get('wow'), ClientOptions(graphics='-d3d12'))
    page.show()
    page.arguments.setText('-d3d11')
    note = page.arguments_note
    assert note.objectName() == 'caution' and not note.isHidden() and 'contradicts DirectX 12' in note.text()
    page.hide()                         # leaving the page saves what was typed
    assert saved[-1].arguments == '-d3d11'
    next(b for b in page.graphics.group.buttons() if b.text() == 'Automatic').click()
    assert note.isHidden()


@pytest.fixture
def bad_launch_options(tmp_path):
    """A settings file without a folder and with launch options that cannot load, for the window fixture."""
    import json
    (tmp_path / 'config_dir').mkdir()
    (tmp_path / 'config_dir/settings.json').write_text(json.dumps({'client_launch': {'wow': {'graphics': ['-d3d11']}}}))


def test_dropped_launch_options_are_reported_before_the_folder_notice(qt, bad_launch_options, window):
    banner = window.client_page.banner
    assert 'invalid and were removed' in banner.text.text() and banner.isVisible()
    banner.close_button.click()
    assert 'Choose an installation folder' in banner.text.text() and banner.isVisible()
    assert banner.action.text() == 'Choose folder'


def test_launch_options_stay_reachable_while_a_client_downloads(qt, window, tmp_path):
    from scuffle.downloads import RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    job = Job('wowf', 'install', status=RUNNING, stage='downloading', done_bytes=1, total_bytes=10)
    window.downloads.jobs.append(job)
    window.downloads._current = job
    window.client_list.select('wowf')
    window.refresh()
    page = window.client_page
    assert page.cancel.isVisibleTo(page) and page.more.isVisibleTo(page)
    assert page.launch_action.isEnabled() and not page.hide_action.isEnabled()
    window.downloads._current = None
    window.downloads.jobs.clear()


def test_a_failed_catalog_check_says_the_list_is_old_and_can_check_again(qt, window, monkeypatch):
    import json
    from pathlib import Path
    from scuffle import catalog, discovery
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    window.on_catalog(window.settings.region, snapshot)
    window.open_settings('clients')
    page = window.settings_page
    assert not page.catalog_row.isVisibleTo(page)
    window.on_catalog(window.settings.region, None)          # the next check fails
    assert page.catalog_row.isVisibleTo(page) and 'could not be reached' in page.catalog_note.text()
    asked = []
    monkeypatch.setattr(discovery, 'fresh', lambda snapshot: True)
    monkeypatch.setattr(window, '_discovering', set())
    monkeypatch.setattr('threading.Thread.start', lambda thread: asked.append(thread.name))
    page.catalog_retry.click()
    assert asked == ['scuffle-catalog'] and window.catalog_state == 'checking'
    assert not page.catalog_row.isVisibleTo(page)
    catalog.apply(None)


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux launch settings')
def test_a_launch_field_being_corrected_survives_reopening_and_the_header_says_so(qt, window):
    window.open_settings('launch')
    page = window.settings_page
    page.launch.environment.text.setPlainText('BAD NAME=1')
    assert 'not saved' in page.saved_note.text()
    window.show_client_page()
    window.open_settings('launch')
    assert page.launch.environment.text.toPlainText() == 'BAD NAME=1'
    assert 'not saved' in page.saved_note.text()
    page.launch.environment.text.setPlainText('GOOD=1')
    assert page.saved_note.text() == 'Changes save automatically.'


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux variables')
def test_reset_clears_a_variables_draft_that_was_never_saved(qt):
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=True)
    page.load(catalog.get('wow'), ClientOptions())
    assert not page.reset_button.isEnabled()
    page.environment.text.setPlainText('1BAD=yes')
    assert page.reset_button.isEnabled()
    page.reset_button.click()
    assert page.environment.text.toPlainText() == '' and not page.reset_button.isEnabled()


def test_a_segmented_row_is_one_tab_stop_so_space_never_changes_the_saved_choice(qt):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=False)
    saved = []
    page.changed.connect(lambda code, options: saved.append(options))
    page.load(catalog.get('wow'), ClientOptions(graphics='-d3d11'))
    page.show()
    chain = page.focus_chain()
    chosen = page.graphics.current()
    assert chosen.text() == 'DirectX 11' and chosen in chain
    assert all(b.focusPolicy() == Qt.ClickFocus for b in page.graphics.group.buttons() if b is not chosen)
    chosen.setFocus()
    QTest.keyClick(chosen, Qt.Key_Space)
    assert page.graphics.value() == '-d3d11'
    QTest.keyClick(chosen, Qt.Key_Left)                    # arrows move the choice
    assert page.graphics.value() == '-d3d12' and page.focusWidget() is page.graphics.current()
    page.hide()


def test_dismissing_a_failed_download_removes_a_client_only_it_kept_listed(qt, window, tmp_path):
    from scuffle.downloads import FAILED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    failed = Job('wowt', 'install', status=FAILED, error='The servers did not answer.')
    window.downloads.jobs.append(failed)
    window.on_job(failed)
    window.client_list.select('wowt')
    assert 'wowt' in window.client_list.rows          # a test client nobody chose, listed for its download
    window.dismiss_notice()
    assert 'wowt' not in window.client_list.rows


def test_a_failed_check_is_named_for_what_failed(qt, window, tmp_path):
    from scuffle.downloads import FAILED, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.downloads.jobs.append(Job('wow', 'verify', status=FAILED, error='3 damaged files'))
    assert window.kept_split()[1] == {'wow': 'Needs repair'}
    window.downloads.jobs.clear()


def test_typing_in_settings_does_not_check_for_updates_each_time(qt, window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'check_updates', lambda: calls.append(1))
    window._update_key = (window.settings.root, window.settings.region, window.settings.locale)
    window.open_settings()
    page = window.settings_page
    page.limit.setValue(5)
    assert calls == []
    page.locale.setCurrentIndex(page.locale.findData('deDE'))
    assert calls == [1]


def test_a_ptr_carrying_another_game_says_which_version_it_runs(qt, window):
    import json
    from pathlib import Path
    from scuffle import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    snapshot['products']['wow_classic_era_ptr']['version'] = '2.5.6.70006'
    window.on_catalog(window.settings.region, snapshot)
    window.open_settings('clients')
    assert window.settings_page.choices['wow_classic_era_ptr']['note'].text() == 'Runs 2.5.6'
    assert window.settings_page.choices['wow_classic_ptr']['note'].text() == ''
    catalog.apply(None)


def test_the_empty_page_offers_its_own_way_back_to_the_clients(qt, window):
    from scuffle import catalog
    window.settings.hidden_clients = [c.code for c in catalog.clients()]
    window.populate()
    window.refresh()
    page = window.client_page
    assert page.focus_chain() == [page.empty_button]
    page.empty_button.click()
    assert window.stack.currentWidget() is window.settings_page and window.settings_page.section == 'clients'


def test_a_missing_wrapper_is_a_caution_and_an_unfinished_argument_shows_what_was_saved(qt, programs_on_path):
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.settings import Settings
    from scuffle.ui.client_options import ClientOptionsPage
    from scuffle.ui.launch_options import LaunchSettings
    launch = LaunchSettings()
    launch.load(Settings())
    launch.wrapper.setText('not-installed --')
    assert launch.wrapper_note.objectName() == 'caution'
    page = ClientOptionsPage(linux=False)
    page.load(catalog.get('wow'), ClientOptions(switches=['-console']))
    page.arguments.setText('"unclosed')
    assert page.preview.text() == 'Saved: adds -console'


def test_transfer_details_keep_their_shape_during_warmup_and_stalls(qt):
    from scuffle.downloads import RUNNING, Job
    from scuffle.ui.widgets import progress_variants
    job = Job('wow', 'install', status=RUNNING, stage='downloading',
              done_bytes=12_400_000_000, total_bytes=31_800_000_000)
    for speed in [0, 48_200_000, 48_400_000, 9_500_000, 0]:
        job.speed = speed
        variants = progress_variants(job)
        assert variants[0].startswith('39% · ') and variants[0].endswith('12.4 GB of 31.8 GB')
        if speed == 0:
            # One estimating note, and the bytes in every length, so known progress never goes.
            assert all(v.endswith('12.4 GB of 31.8 GB') and v.count('Estimating') <= 1 for v in variants)
        else:
            assert [len(v.split(' · ')) for v in variants] == [4, 3, 2]
        if speed >= 10_000_000:
            assert '48 MB/s' in variants[0] and '48.' not in variants[0]
        elif speed:
            assert '9.5 MB/s' in variants[0]
    job.stage = 'materializing'
    assert progress_variants(job) == ['Writing game files…']


def test_queue_reorder_and_bulk_actions_work_from_the_keyboard(qt, window, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from scuffle.downloads import DownloadManager, PAUSED, QUEUED
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    manager = window.downloads
    manager.enqueue('wow', 'install')
    manager.enqueue('wowf', 'install')
    window.open_queue()
    page = window.queue_page
    down = page.cards['wow']['down']
    assert down in page.focus_chain() and down.accessibleName()
    down.setFocus()
    QTest.keyClick(down, Qt.Key_Space)
    qt.processEvents()
    assert page.order == ['wowf', 'wow']
    assert page.cards['wow']['down'] is down
    up = page.cards['wow']['up']
    up.setFocus()
    manager.job('wow').done_bytes = 3
    window.refresh_downloads()
    assert qt.focusWidget() is up
    QTest.keyClick(page.pause_all_button, Qt.Key_Space)
    assert all(j.status == PAUSED for j in manager.jobs)
    QTest.keyClick(page.resume_all_button, Qt.Key_Space)
    assert all(j.status == QUEUED for j in manager.jobs)


@pytest.mark.parametrize('stored', [False, True])
def test_reopened_library_shows_resume_with_honest_progress(qt, window, tmp_path, monkeypatch, stored):
    from scuffle.downloads import DownloadManager, PAUSED
    from scuffle.library import Library
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    monkeypatch.setattr(Library, 'download_progress', lambda self, code: (40, 100) if stored else (0, 0))
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.downloads.enqueue('wow', 'install')
    window.open_library()
    window.client_list.select('wow')
    window.refresh()
    assert window.downloads.job('wow').status == PAUSED
    assert window.client_page.primary.text() == 'Resume'
    assert window.status('wow')[0] == ('Paused at 40%' if stored else 'Paused')
    window.open_queue()
    card = window.queue_page.cards['wow']
    assert card['toggle'].text() == 'Resume'
    assert card['detail'].text().startswith('Paused')
    assert ('40%' in card['detail'].full) == stored
    if not stored:
        assert '%' not in card['detail'].full


def test_game_pause_waits_for_last_game_and_respects_manual_pauses(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DownloadManager, PAUSED, QUEUED
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    manager = window.downloads
    for code in ['wowf', 'wowt']:
        manager.enqueue(code, 'install')
    manager.pause('wowt')
    window.settings.pause_while_playing = True
    window.on_phase('wow', 'Starting')
    assert manager.job('wowf').status == QUEUED
    window.on_phase('wow', 'Running')
    assert manager.job('wowf').status == PAUSED
    window.on_phase('wow_classic', 'Running')
    window.on_task_finished('wow', '')
    assert manager.job('wowf').status == PAUSED
    window.on_task_finished('wow_classic', '')
    assert manager.job('wowf').status == QUEUED
    assert manager.job('wowt').status == PAUSED


def test_download_settings_save_custom_limit_connections_and_policy(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DownloadManager, PAUSED, QUEUED
    from scuffle.settings import Settings
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    manager = window.downloads
    manager.enqueue('wowf', 'install')
    window.on_phase('wow', 'Running')
    window.open_settings('downloads')
    page = window.settings_page
    page.limit.setValue(37)
    page.workers.setValue(7)
    page.pause_while_playing.setChecked(True)
    page.resume_downloads.setChecked(True)
    loaded = Settings.load()
    assert (loaded.bandwidth_limit, loaded.workers) == (37_000_000, 7)
    assert loaded.pause_while_playing and loaded.resume_downloads
    assert manager.throttle.limit == 37_000_000
    with manager._client('wowf', 'eu') as client:
        assert client.workers == 7
    assert manager.job('wowf').status == PAUSED
    page.pause_while_playing.setChecked(False)
    assert manager.job('wowf').status == QUEUED
    assert all(w in page.focus_chain() for w in [page.limit, page.workers,
                                               page.pause_while_playing, page.resume_downloads])


@pytest.mark.parametrize('size', [(900, 560), (960, 650), (1440, 900)])
def test_download_controls_fit_supported_window_sizes(qt, styled, window, size):
    from scuffle.downloads import PAUSED, QUEUED, RUNNING, Job
    page = window.queue_page
    jobs = [Job('wow', 'install', status=RUNNING, total_bytes=100_000_000, speed=100_000),
            Job('wowf', 'install', status=QUEUED, total_bytes=50_000_000),
            Job('wowt', 'install', status=PAUSED, total_bytes=30_000_000)]
    window.resize(*size)
    window.stack.setCurrentWidget(page)
    page.show_jobs(jobs, {j.client: j.client for j in jobs})
    qt.processEvents()
    assert (window.width(), window.height()) == size
    assert page.findChild(QtWidgets.QScrollArea).horizontalScrollBar().maximum() == 0
    for card in page.cards.values():
        for key in ['up', 'down', 'toggle', 'cancel']:
            button = card[key]
            if button.isVisible():
                assert card['frame'].rect().contains(button.geometry())
    settings = window.settings_page
    window.stack.setCurrentWidget(settings)
    settings.show_section('downloads')
    qt.processEvents()
    assert settings.scroll.horizontalScrollBar().maximum() == 0
    for box in [settings.pause_while_playing, settings.resume_downloads]:
        assert box.width() >= box.sizeHint().width()
        assert settings.scroll.viewport().rect().contains(box.mapTo(settings.scroll.viewport(), box.rect().bottomRight()))


def test_queue_total_includes_waiting_sizes_and_qualifies_paused_jobs(qt):
    from scuffle.downloads import PAUSED, QUEUED, RUNNING, Job
    queue = pages.QueuePage()
    jobs = [Job('wow', 'install', status=RUNNING, total_bytes=100_000_000,
                done_bytes=10_000_000, speed=100_000),
            Job('wowf', 'install', status=QUEUED, total_bytes=50_000_000),
            Job('wowt', 'install', status=PAUSED, total_bytes=40_000_000)]
    labels = {j.client: j.client for j in jobs}
    queue.show_jobs(jobs, labels)
    assert '30 min left total if resumed' in queue.summary.text()
    jobs[-1].total_bytes = 0
    queue.show_jobs(jobs, labels)
    assert 'min left' not in queue.summary.text()
    assert '1 not sized yet' in queue.summary.text()


def test_a_start_that_failed_elsewhere_shows_in_the_list_until_it_is_read(qt, window, tmp_path, monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowf'])
    window.populate()
    window.client_list.select('wow')
    window.phases['wowf'] = 'Starting'
    window.on_task_finished('wowf', 'Forever did not start. Check its runtime and try again.')
    assert window.client_list.rows['wowf'].status.text() == 'Did not start'
    window.client_list.select('wowf')
    window.refresh()
    assert 'did not start' in window.client_page.banner.text.text()
    window.dismiss_notice()
    window.refresh()
    assert window.client_list.rows['wowf'].status.text() != 'Did not start'


def test_the_downloads_page_stays_reachable_when_its_only_download_is_on_screen(qt, window, tmp_path):
    from scuffle.downloads import FAILED, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    job = Job('wowf', 'install', status=RUNNING, stage='downloading', done_bytes=1, total_bytes=10)
    window.downloads.jobs.append(job)
    window.downloads._current = job
    window.client_list.select('wowf')
    window.refresh()
    assert window.download_bar.isVisibleTo(window) and window.download_bar.queue.isVisibleTo(window)
    window.downloads._current = None
    job.status = FAILED
    window.client_list.select('wow')
    window.refresh()
    assert window.download_bar.isVisibleTo(window) and 'failed' in window.download_bar.title.text()
    window.downloads.jobs.clear()


def test_the_client_options_header_never_claims_a_field_with_a_mistake_was_saved(qt):
    from scuffle import catalog
    from scuffle.launch_options import ClientOptions
    from scuffle.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=False)
    page.load(catalog.get('wow'), ClientOptions())
    page.arguments.setText('"unclosed')
    page.switches['-console'].setChecked(True)
    assert 'except' in page.saved_note.text()
    page._note_timer.timeout.emit()
    assert 'not saved' in page.saved_note.text()


def test_tab_after_an_arrow_key_moves_on_to_the_next_row(qt, window, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.client_list.select('wow')
    window.refresh()
    window.open_launch_options()
    window.activateWindow()
    page = window.options_page
    window.update_tab_order()          # the order as it was before the arrow key
    first = page.graphics.current()
    first.setFocus(Qt.TabFocusReason)
    QTest.keyClick(first, Qt.Key_Right)
    moved = page.graphics.current()
    assert moved is not first
    # Tab goes from the chosen Graphics button to the chosen Window button, never out of the page.
    assert moved.nextInFocusChain() is not None
    following = moved
    for _ in range(8):
        following = following.nextInFocusChain()
        if following.focusPolicy() & Qt.TabFocus and following.isVisibleTo(window):
            break
    assert following is page.display.current()


def test_the_bar_keeps_no_buttons_while_the_page_shows_its_own_download(qt, window, tmp_path):
    from scuffle.downloads import PAUSED, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    job = Job('wowf', 'install', status=RUNNING, stage='downloading', done_bytes=1, total_bytes=10)
    waiting = Job('wow', 'install', status=PAUSED, done_bytes=1, total_bytes=10)   # it has buttons of its own
    window.downloads.jobs.extend([job, waiting])
    window.downloads._current = job
    window.client_list.select('wowf')
    window.refresh()
    bar = window.download_bar
    assert bar.isVisibleTo(window) and not bar.cancel.isVisibleTo(window) and not bar.toggle.isVisibleTo(window)
    window.downloads._current = None
    window.downloads.jobs.clear()


def test_a_ptr_page_says_when_it_runs_another_games_build(qt, window):
    import json
    from pathlib import Path
    from scuffle import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    snapshot['products']['wow_classic_era_ptr']['version'] = '2.5.6.70006'
    window.on_catalog(window.settings.region, snapshot)
    window.settings.extra_clients = ['wow_classic_era_ptr']
    window.populate()
    window.client_list.select('wow_classic_era_ptr')
    window.refresh()
    assert 'runs a 2.5.6 build' in window.client_page.blurb.text()
    catalog.apply(None)


def test_every_page_starts_at_the_same_height_on_a_tall_window(qt, window):
    window.resize(1440, 900)
    titles = {}
    for name, open_page, title in [('client', window.show_client_page, lambda: window.client_page.title),
                                   ('settings', window.open_settings, lambda: window.settings_page.title_label),
                                   ('queue', window.open_queue, lambda: window.queue_page.title_label)]:
        open_page()
        qt.processEvents()
        widget = title()
        titles[name] = widget.mapTo(window, widget.rect().topLeft()).y()
    assert max(titles.values()) - min(titles.values()) <= 3, titles


def test_the_downloads_page_and_settings_share_one_free_speed_limit(qt, window):
    from scuffle.settings import MEGABYTE, Settings
    window.open_queue()
    window.queue_page.limit.setValue(37)
    assert Settings.load().bandwidth_limit == 37 * MEGABYTE
    window.open_settings('downloads')
    assert window.settings_page.limit.value() == 37 and window.settings_page.limit.text() == '37 MB/s'


@pytest.mark.parametrize('blocker', ['none', 'running', 'external_running', 'queued', 'paused', 'failed', 'pending'])
def test_uninstall_menu_is_only_available_for_idle_installed_clients(qt, window, tmp_path, monkeypatch, blocker):
    from scuffle.downloads import Job
    from scuffle.library import atomic_json
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.downloads.shutdown()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    if blocker == 'running':
        window.phases['wow'] = 'Running'
    elif blocker == 'external_running':
        monkeypatch.setattr(window.library, 'running', lambda code, processes=None: code == 'wow')
        window._running = window.scan_running(window.library)
    elif blocker in {'queued', 'paused', 'failed'}:
        window.downloads.jobs.append(Job('wow', 'update', status=blocker))
    elif blocker == 'pending':
        atomic_json(window.library.client_dir('wow') / 'pending.json', {'phase': 'preparing'})
    window.refresh()
    action = window.client_page.uninstall_action
    assert action.isVisible() and action.isEnabled() == (blocker == 'none')
    if blocker != 'none':
        assert action.toolTip()
        window.open_uninstall()
        assert window.stack.currentWidget() is window.client_page
    window.client_list.select('wowf')
    window.refresh()
    assert not action.isVisible()


@pytest.mark.parametrize('keep', [True, False])
def test_uninstall_confirmation_cancel_and_explicit_choice(qt, window, tmp_path, monkeypatch, keep):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.on_storage({'clients': {'wow': 12_000_000_000}, 'stored': 12_000_000_000})
    requests = []
    monkeypatch.setattr(window.downloads, 'enqueue', lambda *args, **kwargs: requests.append((args, kwargs)))
    window.client_page.uninstall_action.trigger()
    page = window.uninstall_page
    assert window.stack.currentWidget() is page
    assert 'World of Warcraft' in page.title.text() and '12.0 GB' in page.details.text()
    assert 'shared' in page.details.text() and 'Screenshots' in page.keep.text()
    assert page.keep.isChecked() and 'recommended' in page.keep.text()
    page.cancel.click()
    assert not requests and window.stack.currentWidget() is window.client_page
    window.client_page.uninstall_action.trigger()
    page.keep.setChecked(keep)
    page.confirm.click()
    assert requests == [(('wow', 'uninstall'), {'keep_settings': keep})]
    assert window.stack.currentWidget() is window.client_page


def test_uninstall_confirmation_rechecks_if_client_starts(qt, window, tmp_path, monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.open_uninstall()
    window.phases['wow'] = 'Running'
    monkeypatch.setattr(window.downloads, 'enqueue', lambda *a, **k: pytest.fail('Running client was queued'))
    window.confirm_uninstall()
    assert not window.uninstall_page.confirm.isEnabled()
    assert 'Close the client' in window.uninstall_page.problem.text()
    assert window.stack.currentWidget() is window.uninstall_page


@pytest.mark.parametrize('code', ['wow', 'wow_classic_era_ptr'])
def test_uninstall_completion_refreshes_storage_and_not_installed_notice(qt, window, tmp_path, monkeypatch, code):
    from scuffle.downloads import DONE, RUNNING, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.populate()
    window.client_list.select(code)
    job = Job(code, 'uninstall', status=RUNNING, stage='uninstalling')
    window.downloads.jobs.append(job)
    monkeypatch.setattr(window.downloads, 'current', lambda: job if job.status == RUNNING else None)
    window.on_job(job)
    window.client_list.select(code)
    window.refresh()
    assert window.status(code)[0] == 'Uninstalling'
    assert not window.client_page.primary.isEnabled()
    window.client_list.select('wowf')
    window.refresh()
    assert not window.download_bar.toggle.isEnabled() and not window.download_bar.cancel.isEnabled()
    window.open_queue()
    assert not window.queue_page.cards[code]['toggle'].isEnabled()
    assert not window.queue_page.cards[code]['cancel'].isEnabled()
    window.client_list.select(code)
    window.show_client_page()
    measured = []
    monkeypatch.setattr(window, 'measure_storage', lambda: measured.append(True))
    job.status = DONE
    job.result = {'uninstalled': code, 'backup': str(tmp_path / 'Scuffle-backup-wow-test')}
    window.on_job(job)
    assert measured == [True] and window.status(code)[0] == 'Not installed'
    assert 'uninstalled' in window.client_page.banner.text.text()
    assert 'Scuffle-backup-wow-test' in window.client_page.banner.text.text()


def test_adding_an_account_refuses_the_address_of_one_already_signed_in(qt, window, tmp_path, monkeypatch,
                                                                       save_login):
    from scuffle.auth.vault import Vault
    opened = []
    monkeypatch.setattr(pages, 'open_browser', opened.append)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    save_login(Vault(tmp_path / 'data_dir' / 'session', bridge=None), 314159265).save_battle_tag('Wanderer#7395')
    tickets = []

    class Launcher:
        def sign_in(self, version, sign_in):
            tickets.append(sign_in('https://eu.account.battle.net/login/?externalChallenge=login&app=wow'))
    monkeypatch.setattr(window, 'launcher', lambda: Launcher())
    window.sign_in_from_sidebar()           # Add account…
    page = window.sign_in_page
    deadline = time.monotonic() + 5
    while not page.active and time.monotonic() < deadline:
        qt.processEvents()
    # A browser signed in to Battle.net would answer at once for that account, so the user picks the window.
    assert page.active and not page.hint.isHidden() and opened == []
    page.take('http://localhost:0/?ST=EU-0123abcd-314159265&flowTrackingId=', explicit=True)
    assert page.active and 'Wanderer#7395, which is already signed in' in page.status.text()
    page.take('http://localhost:0/?ST=EU-4567cdef-202432841&flowTrackingId=', explicit=True)
    deadline = time.monotonic() + 5
    while not tickets and time.monotonic() < deadline:
        qt.processEvents()
    assert tickets == [b'EU-4567cdef-202432841']


def test_the_wow_account_choice_shows_only_with_several_accounts_and_is_saved(qt, window, tmp_path, save_login):
    from scuffle.auth.vault import Vault
    from scuffle.settings import Settings
    vault = Vault(tmp_path / 'data_dir' / 'session', bridge=None)
    login = save_login(vault, 5)
    main, second = {'id': 11, 'name': 'WANDERER', 'region': 'EU'}, {'id': 12, 'name': 'WoW1', 'region': 'EU'}
    login.save_accounts(5, [main])
    window.refresh()
    assert window.game_account_row.isHidden()
    login.save_accounts(5, [main, second])
    window.refresh()
    assert not window.game_account_row.isHidden()
    assert 'Ask in the game' in window.game_account_button.text()
    actions = {action.text(): action for action in window.game_account_menu().actions()}
    assert actions['Ask in the game'].isChecked()
    actions['WoW1 · EU'].trigger()
    assert Settings.load().game_accounts == {'5': 12} and 'WoW1' in window.game_account_button.text()
    actions = {action.text(): action for action in window.game_account_menu().actions()}
    assert actions['WoW1 · EU'].isChecked() and not actions['Ask in the game'].isChecked()
    actions['Ask in the game'].trigger()
    assert Settings.load().game_accounts == {}
    # Signed out, the choice stays for the same login and the list is gone.
    vault.forget()
    window.refresh()
    assert window.game_account_row.isHidden()



def test_the_sidebar_switches_battle_net_accounts_and_signs_out_of_one(qt, window, tmp_path, save_login):
    from scuffle.auth.vault import Vault
    vault = Vault(tmp_path / 'data_dir' / 'session', bridge=None)
    first = save_login(vault, 5)
    first.save_battle_tag('Wanderer#7395')
    first.save_accounts(5, [{'id': 11, 'name': 'WANDERER', 'region': 'EU'}, {'id': 12, 'name': 'WoW1', 'region': 'EU'}])
    window.refresh()
    # One account reads as before, with the account's name on the button that offers adding another.
    assert 'Signed in' in window.account.text() and window.account_button.text() == 'Wanderer#7395 ▾'
    assert not window.game_account_row.isHidden()
    actions = {action.text(): action for action in window.account_menu().actions()}
    assert list(actions) == ['Wanderer#7395', '', 'Add account…', 'Sign out of this account']
    assert actions['Wanderer#7395'].isChecked()

    second = save_login(vault, 6)       # added and active, without a BattleTag
    window.refresh()
    assert window.account_button.text() == 'Account 2 ▾' and window.game_account_row.isHidden()
    actions = {action.text(): action for action in window.account_menu().actions()}
    assert actions['Account 2'].isChecked() and not actions['Wanderer#7395'].isChecked()
    actions['Wanderer#7395'].trigger()
    assert window.account_button.text() == 'Wanderer#7395 ▾' and not window.game_account_row.isHidden()

    # No switching or signing out while a game starts with the active account's session.
    window.phases['wow'] = 'Connecting'
    window.refresh()
    assert not window.account_button.isEnabled()
    window.phases.clear()
    window.refresh()

    {action.text(): action for action in window.account_menu().actions()}['Sign out of this account'].trigger()
    assert [login.key for login in vault.logins()] == [second.key] and second.has_session()
    assert window.account_button.text() == 'Account 2 ▾'
    {action.text(): action for action in window.account_menu().actions()}['Sign out of this account'].trigger()
    assert window.account.text() == 'Not signed in' and window.account_button.text() == 'Sign in'
    assert window.account_menu() is None

def test_a_running_game_closes_and_shift_turns_the_button_into_force_quit(qt, window, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from scuffle.runtime.game import ASKED, NO_WINDOW
    from scuffle.ui import app as ui
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.downloads.shutdown()
    installed(window, monkeypatch, ['wow'])
    # A game Scuffle did not start counts too, such as one left from an earlier window.
    monkeypatch.setattr(window.library, 'running', lambda code, processes=None: code == 'wow')
    monkeypatch.setattr(window.library, 'game_processes', lambda code: [4242])
    window._running = window.scan_running(window.library)      # as the background scan finds it
    results, calls = [NO_WINDOW, ASKED], []

    def close(executable, processes, *, force=False):
        calls.append((processes, force))
        return results.pop(0)
    monkeypatch.setattr(window, 'launcher', lambda: SimpleNamespace(runtime=SimpleNamespace(close=close)))
    shift = {'held': False}
    monkeypatch.setattr(QApplication, 'queryKeyboardModifiers',
                        staticmethod(lambda: Qt.ShiftModifier if shift['held'] else Qt.NoModifier))
    window.populate()
    window.client_list.select('wow')
    window.refresh()
    page = window.client_page
    assert page.primary.text() == 'Close game' and page.primary.isEnabled()
    assert 'Running' in window.client_list.rows['wow'].status.text()

    def click_and_wait(count):
        page.primary.click()
        deadline = time.monotonic() + 5
        while len(calls) < count:
            qt.processEvents()
            assert time.monotonic() < deadline
        qt.processEvents()
    # A hung game has no window to close, so the banner points at Force quit.
    click_and_wait(1)
    # The worker reports after its call returns, so the banner can come a moment later.
    deadline = time.monotonic() + 5
    while 'Force quit' not in page.banner.text.text() and time.monotonic() < deadline:
        qt.processEvents()
    assert calls[-1] == ([4242], False) and 'Force quit' in page.banner.text.text()
    shift['held'] = True
    window.refresh()
    assert page.primary.text() == 'Force quit' and page.primary.isEnabled()
    click_and_wait(2)
    assert calls[-1] == ([4242], True)
    shift['held'] = False
    window.closing['wow'] = time.monotonic()
    window.refresh()
    assert page.primary.text() == 'Closing…' and not page.primary.isEnabled()
    # A game that has not quit after the grace time gets the same hint.
    monkeypatch.setattr(ui, 'CLOSE_GRACE', 0)
    window.close_overdue('wow', window.closing['wow'])
    assert 'wow' not in window.closing and 'Force quit' in page.banner.text.text()


def test_diagnostics_copy_a_report_and_open_the_proton_log_once_there_is_one(qt, window, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtTest import QTest
    from scuffle.launch import log_dir
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()))
    page = window.settings_page
    window.open_settings('diagnostics')
    assert page.focus_chain()[-1 - len(page.log_buttons):] == [page.copy_button, *page.log_buttons]
    if page.log_buttons:
        assert not page.open_log.isEnabled() and page.log_note.isVisibleTo(page)
        log = log_dir() / 'steam-battlenet.log'
        log.parent.mkdir(parents=True)
        log.write_text('wine: process exited normally\n')
        page.show_section('general')
        page.show_section('diagnostics')        # coming back finds the log a game wrote meanwhile
        assert page.open_log.isEnabled() and not page.log_note.isVisibleTo(page)
        page.open_log.click()
        assert opened == [str(log)]
    qt.clipboard().setText('')
    page.copy_button.setFocus()
    QTest.keyClick(page.copy_button, Qt.Key_Space)
    deadline = time.monotonic() + 20
    while page.collecting and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.01)
    assert qt.clipboard().text().startswith('Scuffle diagnostics\n')
    assert page.saved_note.text() == 'Diagnostics copied. Paste them into your bug report.'
    assert page.copy_button.hasFocus() and page.copy_button.text() == 'Copy diagnostics'


@pytest.fixture
def unseen(qt, tmp_path, monkeypatch):
    """A window as a shortcut starts it: not shown, with an installation folder and Retail installed."""
    from scuffle import discovery, settings as settings_module
    from scuffle.settings import Settings
    from scuffle.ui import app as ui

    def offline(url, *args, **kwargs):
        raise OSError('Tests never reach the version service')
    monkeypatch.setattr(discovery, 'fetch', offline)
    for name in ['config_dir', 'data_dir', 'cache_dir']:
        monkeypatch.setattr(settings_module, name, lambda name=name: tmp_path / name)
        monkeypatch.setattr(ui, name, lambda name=name: tmp_path / name, raising=False)
    Settings(root=str(tmp_path / 'games')).save()
    quits = []
    monkeypatch.setattr(ui.QApplication, 'quit', lambda: quits.append(True))
    window = ui.MainWindow(unseen=True)
    installed(window, monkeypatch, ['wow', 'wow_classic_era'])
    window.populate()
    window.quits = quits
    started = []
    # Play's own work, the update check and the session, is the launcher's. The window decides when to call it.
    monkeypatch.setattr(window, 'play', lambda code, mode=None: (started.append((code, mode)),
                                                                 window.on_phase(code, 'Connecting')))
    window.started = started
    yield window
    window.close()


def settle(qt):
    for _ in range(3):
        qt.processEvents()


@pytest.mark.parametrize('wait', [False, True])
def test_a_shortcut_starts_a_ready_client_without_showing_the_window(qt, unseen, wait):
    unseen.start_shortcut({'play': 'wow_classic_era', 'mode': 'hardcore', 'wait': wait})
    settle(qt)
    assert unseen.started == [('wow_classic_era', 'hardcore')]
    assert not unseen.quits                 # still starting
    unseen.on_phase('wow_classic_era', 'Running')
    settle(qt)
    # Steam counts the game as running while the process lives, so with wait it stays.
    assert bool(unseen.quits) is not wait
    unseen.on_task_finished('wow_classic_era', '')
    settle(qt)
    assert unseen.quits and not unseen.isVisible()


@pytest.mark.parametrize('need', ['update', 'unfinished', 'downloading', 'not-installed', 'hidden-and-behind',
                                  'did-not-start'])
def test_a_shortcut_opens_the_window_on_a_client_that_needs_something_first(qt, unseen, monkeypatch, need):
    from scuffle.downloads import QUEUED, Job
    from scuffle.library import atomic_json
    code = 'wowf' if need == 'not-installed' else 'wow'
    queued = []
    monkeypatch.setattr(unseen.downloads, 'enqueue', lambda client, kind, **kwargs: (
        queued.append((client, kind)), Job(client, kind, status=QUEUED))[1])
    if need in ('update', 'hidden-and-behind'):
        unseen.updates['wow'] = '12.1.7.71000'
    if need == 'hidden-and-behind':
        unseen.settings.unlisted('wow')
        unseen.populate()
    if need == 'unfinished':
        atomic_json(unseen.library.client_dir('wow') / 'pending.json', {'phase': 'downloading'})
    if need == 'downloading':
        unseen.downloads.jobs.append(Job('wow', 'update', status=QUEUED))
    unseen.start_shortcut({'play': code, 'mode': None, 'wait': True})
    if need == 'did-not-start':
        assert unseen.started and not unseen.isVisible()
        unseen.on_task_finished('wow', 'World of Warcraft did not start. Check its runtime and try again.')
    settle(qt)
    assert unseen.isVisible() and unseen.selected() == code
    assert unseen.client_page.banner.isVisible() and unseen.client_page.banner.text.text()
    assert bool(unseen.started) is (need == 'did-not-start')
    assert queued == ([('wow', 'update')] if need in ('update', 'hidden-and-behind') else [])
    # Shown once, the window is the user's, and stays after the shortcut is done.
    assert not unseen.waiters and not unseen.quits


def test_a_shortcut_with_the_tray_setting_on_still_stops_the_queue_when_it_quits(qt, unseen, monkeypatch):
    stopped = []
    monkeypatch.setattr(unseen.downloads, 'shutdown', lambda: stopped.append(True))
    monkeypatch.setattr(unseen.tray, 'available', lambda: True)
    unseen.settings.keep_in_tray = True
    unseen.start_shortcut({'play': 'wow_classic_era', 'mode': None, 'wait': False})
    settle(qt)
    unseen.on_phase('wow_classic_era', 'Running')
    settle(qt)
    # Quitting, not hiding into the tray: the queue stops and is saved as on any quit.
    assert unseen.quits and stopped


def test_a_shortcut_that_needs_a_sign_in_shows_the_sign_in_page(qt, unseen, monkeypatch):
    monkeypatch.setattr(pages, 'open_browser', lambda url: None)
    unseen.start_shortcut({'play': 'wow', 'wait': False})
    assert unseen.started == [('wow', None)] and not unseen.isVisible()
    unseen.on_sign_in_requested('https://example.invalid/login')
    assert unseen.isVisible() and unseen.stack.currentWidget() is unseen.sign_in_page
    unseen.on_sign_in_finished(None)


class Reply:
    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, **message):
        self.sent.append(message)

    def close(self):
        self.closed = True


def test_a_running_window_takes_over_a_shortcut_and_says_when_the_game_has_exited(qt, window, tmp_path,
                                                                                 monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow_classic_era'])
    started = []
    monkeypatch.setattr(window, 'play', lambda code, mode=None: (started.append((code, mode)),
                                                                 window.on_phase(code, 'Connecting')))
    refused = Reply()
    window.on_request({'play': 'wow_classic_era', 'mode': 'arena', 'wait': True}, refused)
    assert refused.sent == [{'error': 'Classic Era has no game mode \'arena\'.'}] and refused.closed
    reply = Reply()
    window.on_request({'play': 'wow_classic_era', 'mode': 'sod', 'wait': True}, reply)
    assert started == [('wow_classic_era', 'sod')]
    # The game's file lets the shortcut keep waiting for it should this window close first.
    executable = str(window.library.root / '_retail_' / 'Wow.exe')
    assert reply.sent == [{'ok': True, 'executable': executable}] and not reply.closed
    window.on_phase('wow_classic_era', 'Running')
    assert not reply.closed
    window.on_task_finished('wow_classic_era', '')
    assert reply.sent == [{'ok': True, 'executable': executable}, {'finished': True}] and reply.closed


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows finds Steam and the Start menu '
                    'outside the folders a test can redirect')
def test_the_menu_adds_a_client_to_the_app_launcher_and_uninstalling_removes_it(qt, window, tmp_path, monkeypatch):
    from scuffle import shortcuts
    from scuffle.downloads import DONE, Job
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.refresh()
    page = window.client_page
    page.more.menu().aboutToShow.emit()
    assert page.launcher_action.isVisible() and page.launcher_action.text() == 'Add to app launcher'
    assert not page.steam_action.isVisible()          # no Steam in this home
    page.launcher_action.trigger()
    assert shortcuts.in_launcher('wow') and 'app launcher' in page.banner.text.text()
    page.more.menu().aboutToShow.emit()
    assert page.launcher_action.text() == 'Remove from app launcher'
    job = Job('wow', 'uninstall', status=DONE, result={'uninstalled': 'wow'})
    window.downloads.jobs.append(job)
    window.on_job(job)
    assert not shortcuts.in_launcher('wow')


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows finds Steam and the Start menu '
                    'outside the folders a test can redirect')
def test_the_menu_adds_a_client_to_steam_once_steam_is_closed(qt, window, tmp_path, monkeypatch):
    from pathlib import Path
    from scuffle import steam
    config = Path.home() / '.local/share/Steam/userdata/5/config'
    config.mkdir(parents=True)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.refresh()
    page = window.client_page
    page.more.menu().aboutToShow.emit()
    assert page.steam_action.isVisible() and page.steam_action.text() == 'Add to Steam'
    monkeypatch.setattr(steam.Steam, 'running', lambda self: True)
    page.steam_action.trigger()
    assert 'Quit Steam' in page.banner.text.text() and not (config / 'shortcuts.vdf').exists()
    monkeypatch.setattr(steam.Steam, 'running', lambda self: False)
    page.banner.action.click()                      # Try again
    entry = steam.shortcut_list(steam.parse((config / 'shortcuts.vdf').read_bytes()))[0][2]
    assert steam.field(entry, 'LaunchOptions').endswith(b'play wow --shortcut --wait')
    assert (config / 'grid' / f'{steam.entry_app_id(entry)}p.png').stat().st_size > 1000
    page.more.menu().aboutToShow.emit()
    assert page.steam_action.text() == 'Remove from Steam'


def test_import_finds_an_installation_and_queues_seeded_installs_for_the_chosen_clients(qt, window, tmp_path,
                                                                                      monkeypatch):
    import time
    from scuffle import imports
    from test_imports import battle_net
    source = battle_net(tmp_path / 'drive/World of Warcraft', {'_retail_': 'wow', '_classic_era_': 'wow_classic_era',
                                                              '_ptr_': 'wowt'})
    monkeypatch.setattr(imports, 'default_places', lambda: [imports.Place(tmp_path / 'drive', 'Mounted drive', 2)])
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    window.populate()
    window.client_list.select('wow')
    window.refresh()
    assert not window.client_page.import_action.isVisible()
    window.client_list.select('wow_classic_era')
    window.refresh()
    requests = []
    monkeypatch.setattr(window.downloads, 'enqueue', lambda *args, **kwargs: requests.append((args, kwargs)))
    window.client_page.import_action.trigger()
    page = window.import_page
    assert window.stack.currentWidget() is page
    deadline = time.monotonic() + 5
    while not page.found and time.monotonic() < deadline:
        qt.processEvents()
    assert [f.path for f in page.found] == [source]
    boxes = page.boxes
    assert boxes['wow_classic_era'].isChecked() and not boxes['wowt'].isChecked()
    assert not boxes['wow'].isEnabled() and 'installed' in boxes['wow'].text()
    assert page.copy.isChecked() and 'WTF and Interface' in page.copy_note.text()
    # Every control is reachable by keyboard, in reading order.
    assert page.focus_chain()[-2:] == [page.cancel, page.confirm]
    assert all(box in page.focus_chain() for box in boxes.values())
    boxes['wowt'].setChecked(True)
    page.copy.setChecked(False)
    page.confirm.click()
    assert requests == [(('wow_classic_era', 'install'), {'seed_from': str(source), 'copy_settings': False}),
                        (('wowt', 'install'), {'seed_from': str(source), 'copy_settings': False})]
    assert window.stack.currentWidget() is window.client_page


def test_import_from_a_chosen_folder_explains_a_folder_without_an_installation(qt, window, tmp_path, monkeypatch):
    import time
    from scuffle import imports
    monkeypatch.setattr(imports, 'default_places', lambda: [])
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.client_list.select('wow')
    window.refresh()
    window.open_import()
    (tmp_path / 'empty').mkdir()
    window.choose_import_folder()
    assert window.stack.currentWidget() is window.import_folder_page
    window.import_folder_page.path.setText(str(tmp_path / 'empty'))
    window.import_folder_page.choose()
    page = window.import_page
    deadline = time.monotonic() + 5
    while not page.problem.text() and time.monotonic() < deadline:
        qt.processEvents()
    assert window.stack.currentWidget() is page
    assert 'No World of Warcraft installation' in page.problem.text()
    assert not page.confirm.isEnabled()


def notifications(window, monkeypatch):
    """What reached the desktop's notification service, as client, title and body."""
    shown = []
    monkeypatch.setattr(window.notifier, 'show',
                        lambda title, body, client, category='', actions=(): shown.append((client, title, body)))
    return shown


def wait_until(qt, check):
    deadline = time.monotonic() + 20         # generous for a loaded machine. Only a failure waits it out
    while not check():
        assert time.monotonic() < deadline
        qt.processEvents()
        time.sleep(0.01)
    qt.processEvents()


def test_a_finished_or_failed_download_notifies_unless_the_window_shows_that_client(qt, window, tmp_path,
                                                                                   monkeypatch):
    from scuffle.downloads import DONE, FAILED, DownloadManager
    from scuffle.library import LibraryError

    def execute(self, job):
        if job.client == 'wowf':
            raise LibraryError('The disk is full.')
        return {}
    monkeypatch.setattr(DownloadManager, '_execute', execute)
    shown = notifications(window, monkeypatch)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.populate()
    window.client_list.select('wow')
    window.show_client_page()
    window.activateWindow()
    wait_until(qt, window.isActiveWindow)
    for code in ['wow', 'wowf', 'wowt']:        # the first one is on screen
        window.downloads.enqueue(code, 'install')
    wait_until(qt, lambda: all(window.downloads.job(c).status in {DONE, FAILED} for c in ['wow', 'wowf', 'wowt']))
    ptr = ui_label('wowt')
    assert shown == [('wowf', 'Forever did not install', 'The disk is full.'),
                     ('wowt', f'{ptr} is installed', 'Ready to play.')]
    # Removing the failure later is no second failure.
    window.downloads.remove('wowf')
    qt.processEvents()
    window.settings.show_notifications = False
    window.downloads.enqueue('wow_classic', 'install')
    wait_until(qt, lambda: window.downloads.job('wow_classic').status == DONE)
    assert len(shown) == 2


def ui_label(code):
    from scuffle import catalog
    return catalog.get(code).label


def test_a_new_version_is_announced_once_also_after_a_restart(qt, window, tmp_path, monkeypatch):
    from scuffle.ui import app as ui
    shown = notifications(window, monkeypatch)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowf'])
    window.populate()
    window.client_list.select('wowf')
    window.on_updates({'wow': '12.0.7.1', 'wowf': False})
    window.on_updates({'wow': '12.0.7.1', 'wowf': False})      # the check half an hour later
    assert shown == [('wow', 'Update for World of Warcraft', 'Version 12.0.7.1 is available.')]
    restarted = ui.MainWindow()
    try:
        later = notifications(restarted, monkeypatch)
        restarted.on_updates({'wow': '12.0.7.1'})
        restarted.on_updates({'wow': '12.0.8.2'})
        assert later == [('wow', 'Update for World of Warcraft', 'Version 12.0.8.2 is available.')]
    finally:
        restarted.close()


@pytest.mark.parametrize('exit_code, report, force, expected', [
    (0, False, False, None),
    (0, True, False, 'Its crash report is in the Errors folder of its game folder.'),
    (0xC0000005, False, False, 'It exited with code 0xc0000005.'),
    (1, False, True, None),          # Force quit ends a game on Windows with code 1
])
def test_a_game_that_quits_unexpectedly_notifies(qt, window, tmp_path, monkeypatch, exit_code, report, force,
                                                 expected):
    import threading
    from types import SimpleNamespace
    from scuffle.ui import app as ui
    shown = notifications(window, monkeypatch)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    monkeypatch.setattr(window.library, 'game_processes', lambda code: [4242])
    release = threading.Event()

    class Game:
        returncode = None

        def wait(self):
            assert release.wait(30)
            if report:
                (tmp_path / 'games/_retail_/Errors').mkdir(parents=True)
                (tmp_path / 'games/_retail_/Errors/crash.txt').write_text('')
            self.returncode = exit_code
    monkeypatch.setattr(ui, 'CDNClient', lambda *args: None)
    monkeypatch.setattr(ui, 'update_available', lambda *args: False)
    runtime = SimpleNamespace(close=lambda *args, **kwargs: 'asked')
    monkeypatch.setattr(window, 'launcher', lambda: SimpleNamespace(
        play=lambda code, sign_in, mode=None, status=None: Game(), runtime=runtime))
    window.populate()
    window.client_list.select('wowf')
    window.play('wow')
    wait_until(qt, lambda: window.phases.get('wow') == 'Running')
    if force:
        window.close_game('wow', force=True)
        qt.processEvents()
    release.set()
    wait_until(qt, lambda: 'wow' not in window.phases)
    assert shown == ([('wow', 'World of Warcraft quit unexpectedly', expected)] if expected else [])


@pytest.mark.parametrize('tray', [True, False])
def test_closing_with_the_tray_setting_keeps_downloads_running_until_quit(qt, window, tmp_path, monkeypatch, tray):
    import json
    from PySide6.QtWidgets import QSystemTrayIcon
    from scuffle import downloads
    from scuffle.downloads import PAUSED, QUEUED, DownloadManager
    from scuffle.settings import Settings
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', staticmethod(lambda: tray))
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)    # the job waits, so its state stays plain
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    window.settings.keep_in_tray = True
    window.downloads.enqueue('wow', 'install')
    try:
        window.close()
        if tray:
            # Hidden with a way back, and the download carries on.
            assert window.isHidden() and window.tray.available()
            assert window.downloads.job('wow').status == QUEUED
            window.quit()
        # Without a tray, closing quits as it always did. Quit from the tray does the same.
        assert window.isHidden() and not window.tray.icon.isVisible()
        assert window.downloads.job('wow').status == PAUSED
        saved = json.loads((downloads.data_dir() / 'downloads.json').read_text())
        assert [(job['client'], job['status']) for job in saved['jobs']] == [('wow', PAUSED)]
        assert Settings.load().keep_in_tray is True
    finally:
        QApplication.setQuitOnLastWindowClosed(True)


def test_without_a_system_tray_the_tray_setting_is_greyed_out_and_keeps_its_choice(qt, window, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    from scuffle.settings import Settings
    available = {'tray': False}
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', staticmethod(lambda: available['tray']))
    window.settings.keep_in_tray = True
    window.open_settings()
    page = window.settings_page
    assert not page.keep_in_tray.isEnabled() and not page.keep_in_tray.isChecked()
    assert page.tray_note.isVisible() and 'no system tray' in page.tray_note.text()
    page.show_notifications.setChecked(False)
    assert (Settings.load().show_notifications, Settings.load().keep_in_tray) == (False, True)
    available['tray'] = True
    window.open_settings()
    assert page.keep_in_tray.isEnabled() and page.keep_in_tray.isChecked() and not page.tray_note.isVisible()
    page.keep_in_tray.setChecked(False)
    assert Settings.load().keep_in_tray is False


def test_the_tray_starts_a_client_as_its_main_button_does(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DownloadManager
    from scuffle.ui import app as ui
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowf'])
    # The update check before a start finds one, so the start becomes that update.
    monkeypatch.setattr(ui, 'CDNClient', lambda *args: None)
    monkeypatch.setattr(ui, 'update_available', lambda *args: True)
    window.populate()
    window.updates['wowf'] = True
    window.refresh()
    actions = window.tray.client_actions
    assert actions['wowf'].text() == 'Update Forever'
    assert actions['wow'].text() == 'Sign in and play World of Warcraft'
    actions['wowf'].trigger()
    assert window.downloads.job('wowf').kind == 'update'
    actions['wow'].trigger()
    assert window.phases['wow'] == 'Checking for updates'
    assert not actions['wow'].isEnabled()           # one start at a time
    wait_until(qt, lambda: window.downloads.job('wow') is not None)
    assert window.downloads.job('wow').kind == 'update' and 'wow' not in window.phases
    assert actions['wow'].text() == 'World of Warcraft · Queued' and not actions['wow'].isEnabled()


def wait_for(qt, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.01)
    return condition()


def test_move_takes_the_installation_to_the_chosen_folder_and_the_window_follows(qt, window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from scuffle.library import Library
    from scuffle.settings import Settings
    source, target = tmp_path / 'games' / 'Scuffle', tmp_path / 'other' / 'Scuffle'
    Library(source).create()
    (source / '_retail_').mkdir()
    (source / '_retail_' / 'Wow.exe').write_bytes(b'inert')
    target.parent.mkdir()
    window.settings.root = str(source)
    window.open_library()
    window.open_settings()
    page = window.settings_page
    assert page.move_button.isVisible() and page.move_button.isEnabled()
    chain = page.focus_chain()
    assert chain[chain.index(page.browse) + 1] is page.move_button
    QTest.mouseClick(page.move_button, Qt.LeftButton)
    assert window.stack.currentWidget() is window.folder_page
    assert window.folder_page.choose_button.text() == 'Move here'
    window.folder_page.path.setText(str(target))
    QTest.mouseClick(window.folder_page.choose_button, Qt.LeftButton)
    confirm = window.move_page.confirm
    assert window.stack.currentWidget() is window.move_page and not confirm.isEnabled()
    assert wait_for(qt, confirm.isEnabled)
    assert 'same drive' in window.move_page.details.text()
    QTest.mouseClick(confirm, Qt.LeftButton)
    assert wait_for(qt, lambda: window.library.root == target and window.moving() is None)
    assert not source.exists() and (target / '_retail_' / 'Wow.exe').read_bytes() == b'inert'
    assert Settings.load().root == str(target) and page.root.text() == str(target)
    assert str(target) in window.client_page.banner.text.text()


@pytest.mark.parametrize('busy', ['job', 'game'])
def test_move_is_offered_only_without_a_download_or_a_game(qt, window, tmp_path, busy):
    from scuffle.downloads import PAUSED, Job
    from scuffle.library import Library
    Library(tmp_path / 'games').create()
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    if busy == 'job':
        window.downloads.jobs.append(Job('wow', 'install', status=PAUSED))
    else:
        window.phases['wow'] = 'Running'
    window.open_settings()
    button = window.settings_page.move_button
    assert button.isVisible() and not button.isEnabled()
    assert ('download' if busy == 'job' else 'Close the game') in button.toolTip()
    window.downloads.jobs.clear()
    window.phases.clear()
    window.refresh()
    assert button.isEnabled()


@pytest.fixture
def crashed_move(tmp_path, monkeypatch):
    """A real move of a real installation, killed in the middle of its copy to another drive.

    Arranged before the window fixture builds the window, with the same private folders."""
    from scuffle import move, settings as settings_module
    from scuffle.settings import Settings
    from test_library import FixtureCDN, SyntheticBuild, run_build
    from test_move import crash
    for name in ['config_dir', 'data_dir', 'cache_dir']:
        monkeypatch.setattr(settings_module, name, lambda name=name: tmp_path / name)
    monkeypatch.setattr(move.system, 'running_executables', lambda *names: [])
    source, target = tmp_path / 'drive-a' / 'Scuffle', tmp_path / 'drive-b' / 'Games'
    run_build(source, FixtureCDN(SyntheticBuild(), tmp_path / 'cdn-cache'))
    Settings(root=str(source)).save()
    target.parent.mkdir()
    assert not crash(40, 'other', source, target)
    assert (source / move.MARKER).is_file() and (target / move.MARKER).is_file()
    return source, target


def test_the_window_undoes_a_move_a_crash_cut_short_before_it_opens_the_folder(qt, crashed_move, window):
    from scuffle import move
    from scuffle.library import Library
    source, target = crashed_move
    assert window.library.root == source and not window.library.moving()
    assert 'undone' in window.client_page.banner.text.text()
    # The worker removes the unfinished copy, and only then does the queue take jobs again.
    assert wait_for(qt, lambda: window.moving() is None and not target.exists())
    assert not move.journal_path().exists()
    with Library(source).locked():
        pass


def offered(window, monkeypatch):
    """The notifications shown, as client, title and the keys of their buttons."""
    shown = []
    monkeypatch.setattr(window.notifier, 'show', lambda title, body, client, category='', actions=():
                        shown.append((client, title, [key for key, _ in actions])))
    return shown


def automatic(window, code):
    job = window.downloads.job(code)
    return (job.kind, job.status, job.automatic) if job else None


def test_the_update_check_queues_updates_only_for_clients_the_user_lets_update(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DownloadManager, PAUSED, QUEUED
    from scuffle.settings import Settings
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowt'])
    shown = offered(window, monkeypatch)
    window.settings.extra_clients = ['wowt']
    window.populate()
    window.client_list.select('wow_classic')        # neither client is on screen
    window.on_updates({'wow': '12.0.7.1', 'wowt': '12.0.8.1'})
    # Without consent nothing downloads, and the notification asks.
    assert window.downloads.jobs == []
    assert shown == [('wow', 'Update for World of Warcraft', ['update', 'always']),
                     ('wowt', f'Update for {ui_label("wowt")}', ['update', 'always'])]
    window.open_settings('downloads')
    page = window.settings_page
    assert page.auto_update in page.focus_chain()
    page.auto_update.setChecked(True)
    assert Settings.load().auto_update is True
    # The switch covers the live client. The test client waits for its own choice.
    assert automatic(window, 'wow') == ('update', QUEUED, True) and automatic(window, 'wowt') is None
    window.show_client_page()
    menu = window.client_page.more.menu()
    window.client_list.select('wowt')
    menu.aboutToShow.emit()
    action = window.client_page.auto_action
    assert action.isVisible() and action.text() == 'Update automatically'
    action.trigger()
    assert automatic(window, 'wowt') == ('update', QUEUED, True) and Settings.load().auto_update_extra == ['wowt']
    # The live client's exception stops its update, which then waits for the user.
    window.client_list.select('wow')
    menu.aboutToShow.emit()
    assert action.text() == "Don't update automatically"
    action.trigger()
    assert automatic(window, 'wow') == ('update', PAUSED, False)
    assert Settings.load().auto_update_except == ['wow']
    window.on_updates({'wow': '12.0.7.1'})
    assert automatic(window, 'wow') == ('update', PAUSED, False)


@pytest.mark.parametrize('choice', ['notification', 'page', 'update-now'])
def test_the_notification_and_the_page_offer_updating_now_or_always(qt, window, tmp_path, monkeypatch, choice):
    from scuffle.downloads import DownloadManager, QUEUED
    from scuffle.settings import Settings
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    offered(window, monkeypatch)
    window.populate()
    window.client_list.select('wowf' if choice != 'page' else 'wow')
    window.on_updates({'wow': '12.0.7.1'})
    window.client_list.select('wow')
    window.show_client_page()
    page = window.client_page
    # Where the notification has no buttons, as the tray's on Windows, the page has the same choice.
    assert page.primary.text() == 'Update' and page.always.isVisible() and page.always in page.focus_chain()
    if choice == 'page':
        page.always.click()
    else:
        window.notifier.acted.emit('wow', 'always' if choice == 'notification' else 'update')
    qt.processEvents()
    assert automatic(window, 'wow') == ('update', QUEUED, choice != 'update-now')
    assert Settings.load().auto_update is (choice != 'update-now')
    assert not page.always.isVisible()


def test_an_automatic_update_waits_while_a_game_starts_or_runs_and_says_so(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DownloadManager, PAUSED, QUEUED
    monkeypatch.setattr(DownloadManager, '_run', lambda self: None)
    window.settings.root = str(tmp_path / 'games')
    window.settings.auto_update = True
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowf'])
    window.populate()
    window.on_updates({'wowf': '12.0.7.1'})
    assert automatic(window, 'wowf') == ('update', QUEUED, True)
    # Retail is only being started, before its game reads anything.
    window.on_phase('wow', 'Checking for updates')
    assert automatic(window, 'wowf') == ('update', PAUSED, True)
    window.client_list.select('wowf')
    window.refresh()
    assert window.status('wowf') == ('Waiting for games', 'attention')
    assert window.client_page.phase.full == 'Starts once every game in this folder has closed'
    window.on_task_finished('wow', '')
    window.on_running_scanned(window.library, set())
    assert automatic(window, 'wowf') == ('update', QUEUED, True)
    # A game Scuffle did not start, found by the scan of the processes.
    window.on_running_scanned(window.library, {'wow'})
    assert automatic(window, 'wowf') == ('update', PAUSED, True)
    window.on_running_scanned(window.library, set())
    assert automatic(window, 'wowf') == ('update', QUEUED, True)


def test_an_automatic_update_says_its_version_when_done_and_its_failure_once(qt, window, tmp_path, monkeypatch):
    from scuffle.downloads import DONE, FAILED, DownloadManager
    from scuffle.library import LibraryError

    def execute(self, job):
        if job.client == 'wowf':
            raise LibraryError('Not enough free space: about 80 GB are needed')
        return {}
    monkeypatch.setattr(DownloadManager, '_execute', execute)
    window.settings.root = str(tmp_path / 'games')
    window.settings.auto_update = True
    window.open_library()
    installed(window, monkeypatch, ['wow', 'wowf'])
    shown = notifications(window, monkeypatch)
    window.populate()
    window.client_list.select('wow_classic')        # neither client is on screen
    window.on_updates({'wow': '12.1.5.70575', 'wowf': '12.0.7.1'})
    wait_until(qt, lambda: all(window.downloads.job(c).status in {DONE, FAILED} for c in ['wow', 'wowf']))
    window.on_updates({'wow': False, 'wowf': '12.0.7.1'})         # the next check, half an hour later
    qt.processEvents()
    assert shown == [('wow', 'World of Warcraft updated to 12.1.5.70575', 'Updated automatically. Ready to play.'),
                     ('wowf', 'Forever did not update', 'Not enough free space: about 80 GB are needed')]
    assert window.downloads.job('wowf').status == FAILED


def test_the_tray_says_when_the_next_check_runs_and_checks_now(qt, window, tmp_path, monkeypatch):
    window.settings.root = str(tmp_path / 'games')
    window.open_library()
    installed(window, monkeypatch, ['wow'])
    checks = []
    monkeypatch.setattr(window, 'check_updates', lambda: checks.append(True))
    window.update_timer.start(270_000)        # 4.5 minutes, as the minute starts
    window.refresh()
    tray = window.tray
    assert tray.next_check.text() == 'Next check in 5 min' and not tray.next_check.isEnabled()
    tray.check_action.trigger()
    assert checks == [True]
    window.refresh()
    assert tray.next_check.text() == 'Next check in 30 min'


def test_a_start_at_sign_in_stays_in_the_tray_and_shows_the_window_without_one(qt, window, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    from scuffle.ui import app as ui
    available = {'tray': True}
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', staticmethod(lambda: available['tray']))
    try:
        window.hide()
        window.start_in_tray()
        window.check_tray()
        assert window.isHidden()
        # A second start at sign-in, or the entry run again, leaves it there.
        reply = Reply()
        window.on_request({'tray': True}, reply)
        assert window.isHidden() and reply.sent == [{'ok': True}]
        # A tray bar that starts later gets a moment. One that never comes shows the window.
        available['tray'] = False
        window.check_tray()
        assert window.isHidden()
        monkeypatch.setattr(ui, 'TRAY_GRACE', 0)
        window.start_in_tray()
        window.check_tray()
        assert window.isVisible()
    finally:
        QApplication.setQuitOnLastWindowClosed(True)


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows keeps the entry in the registry')
def test_starting_at_sign_in_writes_and_removes_only_scuffles_own_entry(qt, window):
    from pathlib import Path
    entry = Path(os.environ['XDG_CONFIG_HOME']) / 'autostart' / 'scuffle.desktop'
    window.open_settings()
    page = window.settings_page
    assert page.sign_in_start in page.focus_chain() and not page.sign_in_start.isChecked()
    page.sign_in_start.setChecked(True)
    text = entry.read_text()
    assert 'X-Scuffle-Autostart=true\n' in text
    assert next(line for line in text.splitlines() if line.startswith('Exec=')).endswith(' --tray')
    window.open_settings()
    assert page.sign_in_start.isChecked()
    page.sign_in_start.setChecked(False)
    assert not entry.exists()
    # A file of the same name that Scuffle did not write is neither replaced nor removed.
    entry.write_text('[Desktop Entry]\nType=Application\nExec=my-own-scuffle\n')
    page.sign_in_start.setChecked(True)
    assert entry.read_text() == '[Desktop Entry]\nType=Application\nExec=my-own-scuffle\n'
    assert not page.sign_in_start.isChecked() and 'not made by Scuffle' in page.sign_in_note.text()
    window.set_start_at_sign_in(False)
    assert entry.exists()


def self_update_release(asset='Scuffle.exe'):
    from scuffle import selfupdate
    return selfupdate.Release('9.0.0', f'{selfupdate.PAGES}tag/v9.0.0', asset, 'u', 1, 's' if asset else '')


def found_self_update(qt, window, monkeypatch, release):
    from scuffle import selfupdate
    monkeypatch.setattr(selfupdate, 'latest', lambda: release)
    window.self_checked = None
    window.check_self_update()
    deadline = time.monotonic() + 5
    while window.self_release is not release and time.monotonic() < deadline:
        qt.processEvents()
    window.refresh()
    assert window.self_release is release


def test_a_scuffle_update_waits_for_the_game_then_installs_and_restarts(qt, window, monkeypatch):
    from scuffle import selfupdate
    installed, quit_calls = [], []
    monkeypatch.setattr(selfupdate, 'install', lambda release, progress: installed.append(release.version))
    monkeypatch.setattr(window, 'quit', lambda: quit_calls.append(True))
    found_self_update(qt, window, monkeypatch, self_update_release())
    page = window.settings_page
    assert '9.0.0 is out' in window.client_page.banner.text.text()
    assert window.client_page.banner.action.text() == 'Update and restart'
    assert window.self_update_menu_text() == 'Update Scuffle to 9.0.0'

    window.phases['wow'] = 'Running'
    window.client_page.banner.action.click()
    window.refresh()
    qt.processEvents()
    assert installed == [] and window.self_state == 'waiting' and 'when the game is finished' in page.self_update_note.text()
    assert not page.self_update_now.isEnabled()

    window.phases.clear()
    window.refresh()
    assert wait_for(qt, lambda: quit_calls)
    assert installed == ['9.0.0'] and window.restart_after_quit


def test_a_scuffle_update_never_restarts_over_a_game_that_started_while_it_downloaded(qt, window, monkeypatch):
    from scuffle import selfupdate
    quit_calls = []
    monkeypatch.setattr(selfupdate, 'install', lambda release, progress: None)
    monkeypatch.setattr(window, 'quit', lambda: quit_calls.append(True))
    found_self_update(qt, window, monkeypatch, self_update_release())
    window.start_self_update()
    window.phases['wow'] = 'Running'
    assert wait_for(qt, lambda: window.self_state == 'installed')
    window.refresh()
    assert not quit_calls and 'It is installed' in window.settings_page.self_update_note.text()
    window.phases.clear()
    window.refresh()
    assert quit_calls and window.restart_after_quit


def test_a_failed_scuffle_update_says_why_and_can_be_tried_again(qt, window, monkeypatch):
    from scuffle import selfupdate

    def fails(release, progress):
        raise selfupdate.UpdateError('The download does not match the checksum of the release')
    monkeypatch.setattr(selfupdate, 'install', fails)
    found_self_update(qt, window, monkeypatch, self_update_release())
    window.start_self_update()
    assert wait_for(qt, lambda: 'could not update itself' in window.client_page.banner.text.text())
    assert 'checksum' in window.client_page.banner.text.text()
    assert window.self_state == '' and not window.restart_after_quit and window.settings_page.self_update_now.isEnabled()


def test_a_skipped_scuffle_version_stays_quiet(qt, window, monkeypatch):
    release = self_update_release()
    found_self_update(qt, window, monkeypatch, release)
    window.settings_page.self_update_skip.click()
    assert window.settings.skipped_self_update == '9.0.0' and window.self_update_menu_text() == ''
    assert not window.messages.get('') and 'You skipped' in window.settings_page.self_update_note.text()
    window.on_self_update_found(release)
    assert not window.messages.get('')


def test_a_managed_installation_is_only_told_about_a_new_scuffle(qt, window, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    found_self_update(qt, window, monkeypatch, self_update_release(asset=''))
    assert window.client_page.banner.action.text() == 'Release notes' and window.self_update_menu_text() == ''
    assert window.settings_page.self_update_now.isHidden()
    window.client_page.banner.action.click()
    assert opened == ['https://github.com/nyrda/scuffle/releases/tag/v9.0.0']


def test_the_update_check_stays_off_when_the_user_turned_it_off(qt, window, monkeypatch):
    from scuffle import selfupdate
    asked = []
    monkeypatch.setattr(selfupdate, 'latest', lambda: asked.append(True))
    window.settings.check_self_updates = False
    window.self_checked = None
    window.check_self_update()
    for _ in range(20):
        qt.processEvents()
    assert not asked
