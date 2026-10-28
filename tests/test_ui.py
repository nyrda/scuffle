"""Check the sign-in page's clipboard handling with a real Qt clipboard."""
import os
import sys

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')
QApplication = QtWidgets.QApplication

from waystone.ui import pages  # noqa: E402


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


def test_mode_buttons_are_replaced_not_added_when_switching_clients(qt):
    from waystone.ui.widgets import Segmented
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
    from waystone import discovery, settings as settings_module
    from waystone.ui import app as ui

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
    from waystone.downloads import RUNNING, Job
    queue = pages.QueuePage()
    job = Job('wow', 'install', status=RUNNING, done_bytes=1, total_bytes=100)
    queue.show_jobs([job], {'wow': 'World of Warcraft'})
    pause = queue.cards['wow']['toggle']
    job.done_bytes = 2
    queue.show_jobs([job], {'wow': 'World of Warcraft'})
    # The same button, so keyboard focus on it is not lost every percent.
    assert queue.cards['wow']['toggle'] is pause
    assert '2%' in queue.cards['wow']['detail'].text()


def test_narrow_download_bar_keeps_its_progress_and_shortens_the_details_whole(qt):
    from waystone.downloads import RUNNING, Job
    from waystone.ui.widgets import DownloadBar
    bar = DownloadBar()
    job = Job('wow_classic', 'install', status=RUNNING, done_bytes=46_200_000_000,
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
    from waystone.downloads import DONE, QUEUED, Job
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
    from waystone.ui import app as ui
    monkeypatch.setattr(ui, 'cache_dir', lambda: tmp_path / 'style')
    ui.prepare(qt)
    from waystone import launch
    from waystone.runtime.proton import Proton
    from waystone.ui import launch_options
    build = Proton('GE-Proton10-25 with a very long name from a custom folder', tmp_path / 'proton')
    build.path.mkdir()
    # Both the list and the build a launch would pick, so nothing depends on this machine's builds.
    monkeypatch.setattr(launch_options, 'find_protons', lambda: [build])
    monkeypatch.setattr(launch, 'find_protons', lambda: [build])


def test_settings_never_scroll_sideways_at_the_minimum_size(qt, styled, window):
    import json
    from pathlib import Path
    from PySide6.QtWidgets import QScrollArea
    from waystone import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    window.on_catalog(window.settings.region, snapshot)
    window.settings.show_other_clients = True
    window.open_settings()
    window.settings_page.load(window.settings, [c.code for c in catalog.CLIENTS if c.test],
                              {'wowlivetest': 'Install failed'})
    window.resize(960, 650)
    for section, _ in window.settings_page.SECTIONS:
        window.settings_page.show_section(section)
        for _ in range(3):
            qt.processEvents()
        assert window.settings_page.findChild(QScrollArea).horizontalScrollBar().maximum() == 0, section
    catalog.apply(None)


def test_the_folder_picker_reveals_the_current_folder_once_it_has_loaded(qt, tmp_path):
    import time
    for name in ['alpha', 'beta', 'gamma', *(f'filler {n:02}' for n in range(40)), 'zeta']:
        (tmp_path / name).mkdir()
    target = tmp_path / 'zeta'
    page = pages.FolderPage()
    page.resize(700, 400)
    page.show()
    page.start(str(target))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and page.model.filePath(page.tree.currentIndex()) != str(target):
        qt.processEvents()
    for _ in range(20):
        qt.processEvents()
        time.sleep(0.01)
    rect = page.tree.visualRect(page.tree.currentIndex())
    assert page.model.filePath(page.tree.currentIndex()) == str(target)
    assert 0 <= rect.top() and rect.bottom() <= page.tree.viewport().height()


def test_a_queued_client_offers_nothing_to_pause(qt, window, tmp_path):
    from waystone.downloads import QUEUED, RUNNING, Job
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
    from waystone.downloads import FAILED, Job
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
    from waystone.settings import Settings
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
    from waystone.downloads import FAILED, Job
    queue = pages.QueuePage()
    queue.show_jobs([Job('wow', 'install', status=FAILED, error='The servers did not answer.')],
                    {'wow': 'World of Warcraft'})
    card = queue.cards['wow']
    assert 'The servers did not answer.' in card['detail'].text()
    assert card['toggle'].text() == 'Try again' and card['cancel'].text() == 'Remove'


def test_progress_after_the_transfer_names_the_stage_instead_of_a_time(qt):
    from waystone.downloads import RUNNING, Job
    from waystone.ui.widgets import progress_variants
    job = Job('wow', 'install', status=RUNNING, done_bytes=100, total_bytes=100, speed=40, stage='materializing')
    assert progress_variants(job) == ['Writing game files…']
    job.stage, job.total_bytes = 'seeding_cdn_index', 0
    assert progress_variants(job) == ['Fetching download indexes…']


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
    from waystone.downloads import RUNNING, Job
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
    from waystone.downloads import FAILED, Job
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
    from waystone.downloads import FAILED, Job
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
    from waystone.downloads import QUEUED, Job
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
    from waystone.downloads import DONE, RUNNING, Job
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
    from waystone.downloads import QUEUED, RUNNING, Job
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
    from waystone.downloads import FAILED, PAUSED, Job
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
    from waystone.downloads import QUEUED, RUNNING, Job
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
    from waystone.downloads import PAUSED, QUEUED, RUNNING, Job
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
    # The sections are one Tab stop, the chosen one. Arrow keys move between them.
    assert chain[:3] == [page.close_button, page.sections.current(), page.browse] and page.limit not in chain
    page.show_section('launch')
    extra = QComboBox()
    page.add_launch_widget(extra)       # how launch options built elsewhere join the page
    qt.processEvents()
    chain = page.focus_chain()
    assert chain[-1] is extra and page.browse not in chain
    if page.proton is not None:
        assert chain.index(page.proton) == 2
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
    from waystone.ui import app
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        app.close_on_signals(window)
        os.kill(os.getpid(), signal.SIGINT)
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
    from waystone.settings import Settings
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
    from waystone.downloads import FAILED, Job
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
    from waystone import catalog
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
    from waystone import catalog
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
    from waystone.downloads import QUEUED, Job
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
    from waystone import catalog, discovery, settings as settings_module
    from waystone.ui import app as ui
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
    from waystone import catalog, discovery
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    discovery.save(tmp_path / 'cache_dir', snapshot)
    window.open_settings()
    page = window.settings_page
    assert window.settings.region == 'eu' and 'wowlivetest' not in page.choices
    page.region.setCurrentIndex(page.region.findData('us'))
    assert 'wowlivetest' in page.choices and catalog.release('wow').version == '12.1.5.70575'
    catalog.apply(None)


def test_a_finished_job_no_longer_keeps_its_client_listed_or_locked(qt, window, tmp_path, monkeypatch):
    from waystone.downloads import DONE, Job
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
    from waystone import catalog
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
    from waystone import catalog
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
    from waystone.ui.widgets import FactsCard
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
    from waystone.launch_options import ClientOptions
    from waystone.settings import Settings
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
    from waystone.settings import Settings
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
    from waystone.launch_options import LinuxOptions
    from waystone.settings import Settings
    window.settings.linux_launch = LinuxOptions(gamemode=True, wayland=True)
    window.open_settings()
    launch = window.settings_page.launch
    # No gamemoderun, and a Proton build without Wayland or an ntsync switch.
    for name, note in [('gamemode', 'gamemoderun was not found'), ('wayland', 'Not in this Proton build'),
                       ('ntsync', 'Not adjustable')]:
        box = launch.boxes[name]
        assert not box.isChecked() and not box.isEnabled() and note in launch.notes[name].text()
    launch.boxes['log'].setChecked(True)
    saved = Settings.load().linux_launch
    assert saved.log and saved.gamemode and saved.wayland and saved.ntsync
    # Once gamemoderun is installed, the saved choice shows again.
    (programs_on_path / 'gamemoderun').write_text('#!/bin/sh\n')
    (programs_on_path / 'gamemoderun').chmod(0o755)
    launch.update_notes()
    assert launch.boxes['gamemode'].isChecked() and launch.boxes['gamemode'].isEnabled()


def test_the_client_page_without_linux_saves_arguments_and_keeps_hidden_variables(qt):
    from waystone import catalog
    from waystone.launch_options import ClientOptions
    from waystone.ui.client_options import ClientOptionsPage
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
    from waystone import catalog
    from waystone.launch_options import ClientOptions
    from waystone.ui.client_options import ClientOptionsPage
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
    from waystone.downloads import RUNNING, Job
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
    from waystone import catalog, discovery
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
    assert asked == ['waystone-catalog'] and window.catalog_state == 'checking'
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
    from waystone import catalog
    from waystone.launch_options import ClientOptions
    from waystone.ui.client_options import ClientOptionsPage
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
    from waystone import catalog
    from waystone.launch_options import ClientOptions
    from waystone.ui.client_options import ClientOptionsPage
    page = ClientOptionsPage(linux=False)
    saved = []
    page.changed.connect(lambda code, options: saved.append(options))
    page.load(catalog.get('wow'), ClientOptions(graphics='-d3d11'))
    page.show()
    chain = page.focus_chain()
    chosen = page.graphics.current()
    assert chosen.text() == 'DirectX 11' and chain[2] is chosen
    assert all(b.focusPolicy() == Qt.ClickFocus for b in page.graphics.group.buttons() if b is not chosen)
    chosen.setFocus()
    QTest.keyClick(chosen, Qt.Key_Space)
    assert page.graphics.value() == '-d3d11'
    QTest.keyClick(chosen, Qt.Key_Left)                    # arrows move the choice
    assert page.graphics.value() == '-d3d12' and page.focusWidget() is page.graphics.current()
    page.hide()


def test_dismissing_a_failed_download_removes_a_client_only_it_kept_listed(qt, window, tmp_path):
    from waystone.downloads import FAILED, Job
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
    from waystone.downloads import FAILED, Job
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
    from waystone import catalog
    snapshot = json.loads((Path(__file__).parent / 'fixtures/catalog-us.json').read_text())
    snapshot['products']['wow_classic_era_ptr']['version'] = '2.5.6.70006'
    window.on_catalog(window.settings.region, snapshot)
    window.open_settings('clients')
    assert window.settings_page.choices['wow_classic_era_ptr']['note'].text() == 'Runs 2.5.6'
    assert window.settings_page.choices['wow_classic_ptr']['note'].text() == ''
    catalog.apply(None)


def test_the_empty_page_offers_its_own_way_back_to_the_clients(qt, window):
    from waystone import catalog
    window.settings.hidden_clients = [c.code for c in catalog.clients()]
    window.populate()
    window.refresh()
    page = window.client_page
    assert page.focus_chain() == [page.empty_button]
    page.empty_button.click()
    assert window.stack.currentWidget() is window.settings_page and window.settings_page.section == 'clients'


def test_a_missing_wrapper_is_a_caution_and_an_unfinished_argument_shows_what_was_saved(qt, programs_on_path):
    from waystone import catalog
    from waystone.launch_options import ClientOptions
    from waystone.settings import Settings
    from waystone.ui.client_options import ClientOptionsPage
    from waystone.ui.launch_options import LaunchSettings
    launch = LaunchSettings()
    launch.load(Settings())
    launch.wrapper.setText('not-installed --')
    assert launch.wrapper_note.objectName() == 'caution'
    page = ClientOptionsPage(linux=False)
    page.load(catalog.get('wow'), ClientOptions(switches=['-console']))
    page.arguments.setText('"unclosed')
    assert page.preview.text() == 'Saved: adds -console'


def test_transfer_details_keep_their_shape_during_warmup_and_stalls(qt):
    from waystone.downloads import RUNNING, Job
    from waystone.ui.widgets import progress_variants
    job = Job('wow', 'install', status=RUNNING, stage='downloading',
              done_bytes=12_400_000_000, total_bytes=31_800_000_000)
    for speed in [0, 48_200_000, 48_400_000, 9_500_000, 0]:
        job.speed = speed
        variants = progress_variants(job)
        assert [len(v.split(' · ')) for v in variants] == [4, 3, 2]
        assert variants[0].startswith('39% · ') and variants[0].endswith('12.4 GB of 31.8 GB')
        assert ('Estimating… · Estimating…' in variants[0]) == (speed == 0)
        if speed >= 10_000_000:
            assert '48 MB/s' in variants[0] and '48.' not in variants[0]
        elif speed:
            assert '9.5 MB/s' in variants[0]
    job.stage = 'materializing'
    assert progress_variants(job) == ['Writing game files…']


def test_queue_reorder_and_bulk_actions_work_from_the_keyboard(qt, window, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from waystone.downloads import DownloadManager, PAUSED, QUEUED
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
    from waystone.downloads import DownloadManager, PAUSED
    from waystone.library import Library
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
    from waystone.downloads import DownloadManager, PAUSED, QUEUED
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
    from waystone.downloads import DownloadManager, PAUSED, QUEUED
    from waystone.settings import Settings
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
    from waystone.downloads import PAUSED, QUEUED, RUNNING, Job
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
    from waystone.downloads import PAUSED, QUEUED, RUNNING, Job
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
