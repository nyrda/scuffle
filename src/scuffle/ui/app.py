"""The Scuffle window."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QMainWindow, QPushButton, QStackedWidget,
                               QMenu, QStyleFactory, QVBoxLayout, QWidget)

from .. import battlenet, catalog, diagnostics, discovery, imports, instance, move, shortcuts, steam, system
from ..downloads import DONE, FAILED, PAUSED, QUEUED, RUNNING, DownloadManager, Job, MoveTask
from ..launch import Launcher, LaunchError
from ..library import Library, LibraryError, update_available
from ..ngdp.cdn import CDNClient
from ..settings import LOCALE_NAMES, MEGABYTE, REGIONS, Settings, cache_dir, data_dir
from .client_options import ClientOptionsPage, summary as options_summary
from .notifications import Notifier
from .pages import ClientPage, FolderPage, ImportPage, MovePage, QueuePage, SettingsPage, SignInPage, UninstallPage
from .style import SPACING, capsule_image, dark_palette, make_images, png, size_text, stylesheet, tile_image
from .tray import PLAY_LIMIT, Tray
from .widgets import ClientList, DownloadBar, fixed, label, progress_variants


CLOSE_GRACE = 20      # seconds a game gets to quit before Force quit is suggested
CHECK_INTERVAL = 30 * 60      # seconds between update checks
TRAY_GRACE = 20       # seconds a start in the tray waits for a tray bar that starts after Scuffle


def post(signal, *args) -> None:
    """Emit a Bus signal from a worker thread. Once the window is gone there is no one left to tell.

    A thread can finish after the window closed, such as the running-games scan while the app quits.
    """
    try:
        signal.emit(*args)
    except RuntimeError as error:
        if 'deleted' not in str(error):
            raise


class Bus(QObject):
    """Signals from worker threads to the window."""
    job_changed = Signal(object)
    sign_in_requested = Signal(str)
    updates_checked = Signal(dict)
    launch_phase = Signal(str, str)      # client, phase text
    task_finished = Signal(str, str)     # client or '' for sign-in, error text or ''
    update_needed = Signal(str)
    storage_ready = Signal(dict)
    catalog_checked = Signal(str, object)   # region, what discovery found or None
    game_closed = Signal(str, str, bool)    # client, what closing did or an error, forced
    running_scanned = Signal(object, object)   # library, clients whose game runs
    diagnostics_ready = Signal(str)            # the report, or '' when it could not be built
    request_received = Signal(object, object)  # a later start's request, the way to answer it
    import_found = Signal(int, object)      # search number, an installation to import
    import_searched = Signal(int, bool)     # search number, whether it looked everywhere
    import_picked = Signal(int, object, str)   # search number, installations in a chosen folder, or why none
    download_finished = Signal(object, str)    # a job that ran to its end, DONE or FAILED
    # Client, exit code, whether it left a crash report. Windows codes such as 0xC0000005 pass int32.
    game_exited = Signal(str, object, bool)
    move_changed = Signal(object)           # the MoveTask of the installation folder
    move_previewed = Signal(str, dict)      # the folder asked about, what moving there takes


class MainWindow(QMainWindow):
    def __init__(self, *, unseen: bool = False):
        super().__init__()
        self.settings = Settings.load()
        # Started by a shortcut, and not shown yet. The window shows once it has something to say.
        self.unseen = unseen
        self.leaving = False        # the shortcut that started this process is done with it
        self._closing = False
        self.waiters: list[tuple] = []          # (client, wait for its exit, called when done)
        self.from_shortcut: set[str] = set()   # clients a shortcut is starting
        # Before the library opens, so an interrupted move opens the folder it left complete.
        self.move_notice = self.recover_move()
        self.bus = Bus()
        self.updates: dict[str, bool] = {}
        self.phases: dict[str, str] = {}      # client -> Checking for updates, Signing in, Starting, Running
        self.closing: dict[str, float] = {}   # client -> when it was asked to close
        self._running: set[str] = set()       # clients whose game runs, whoever started it
        self._running_at = float('-inf')
        self._scanning = False
        self.library = self.downloads = None
        self._sign_in_result: list = []
        self._sign_in_done = threading.Event()
        self._signing_in = False
        self._runs: dict[str, tuple] = {}     # client -> its job and the status it last reported
        self.forced: set[str] = set()         # clients force quit since they started
        self._quitting = self._in_tray = False
        self.bus.job_changed.connect(self.on_job)
        self.bus.download_finished.connect(self.on_download_finished)
        self.bus.game_exited.connect(self.on_game_exited)
        self.bus.sign_in_requested.connect(self.on_sign_in_requested)
        self.bus.updates_checked.connect(self.on_updates)
        self.bus.launch_phase.connect(self.on_phase)
        self.bus.task_finished.connect(self.on_task_finished)
        self.bus.update_needed.connect(self.on_update_needed)
        self.bus.storage_ready.connect(self.on_storage)
        self.bus.catalog_checked.connect(self.on_catalog)
        self.bus.game_closed.connect(self.on_game_closed)
        self.bus.running_scanned.connect(self.on_running_scanned)
        self.bus.diagnostics_ready.connect(self.on_diagnostics)
        self.bus.request_received.connect(self.on_request)
        self.bus.import_found.connect(self.on_import_found)
        self.bus.import_searched.connect(self.on_import_searched)
        self.bus.import_picked.connect(self.on_import_picked)
        self._import_search = 0         # the current search. Results of an older one are dropped
        self._import_searching = False
        self.bus.move_changed.connect(self.on_move)
        self.bus.move_previewed.connect(self.on_move_preview)
        # Shift turns Close game into Force quit while it is held.
        QApplication.instance().installEventFilter(self)
        self.storage: dict = {}
        # The last lists from the version service and Battle.net, so the window starts without waiting.
        discovery.restore(cache_dir(), self.settings.region)
        self.catalog_state = 'ready' if catalog.checked() else 'checking'
        self._discovering: set[str] = set()     # regions being checked
        self._catalog_region = self.settings.region
        self._listed: list = []
        self.open_library()

        self.setWindowTitle('Scuffle')
        self.resize(980, 620)
        self.setMinimumSize(900, 560)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.build_sidebar())
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        self.client_page = ClientPage()
        self.client_page.primary_clicked.connect(self.primary_action)
        self.client_page.cancel_clicked.connect(lambda: self.downloads.cancel(self.selected()))
        self.client_page.verify_clicked.connect(self.verify)
        self.client_page.open_folder_clicked.connect(self.open_folder)
        self.client_page.mode_changed.connect(self.remember_mode)
        self.client_page.hide_clicked.connect(self.hide_client)
        self.client_page.uninstall_clicked.connect(self.open_uninstall)
        self.client_page.launcher_clicked.connect(self.toggle_launcher)
        self.client_page.steam_clicked.connect(self.toggle_steam)
        self.client_page.auto_update_clicked.connect(self.toggle_auto_update)
        self.client_page.always_update_clicked.connect(lambda: self.selected() and self.always_update(self.selected()))
        self.client_page.more.menu().aboutToShow.connect(self.update_shortcut_actions)
        self.client_page.more.menu().aboutToShow.connect(self.update_auto_action)
        self.uninstall_page = UninstallPage()
        self.uninstall_page.cancelled.connect(self.show_client_page)
        self.uninstall_page.confirmed.connect(self.confirm_uninstall)
        self.client_page.import_clicked.connect(self.open_import)
        self.import_page = ImportPage()
        self.import_page.cancelled.connect(self.close_import)
        self.import_page.confirmed.connect(self.confirm_import)
        self.import_page.choose_folder.connect(self.choose_import_folder)
        self.import_folder_page = FolderPage(
            'Choose the installation to import', 'Pick the World of Warcraft folder, such as the one Battle.net '
            'installed, or a folder above it. Scuffle only reads it.', existing_only=True)
        self.import_folder_page.chosen.connect(self.on_import_folder_chosen)
        self.import_folder_page.cancelled.connect(lambda: self.stack.setCurrentWidget(self.import_page))
        self.client_page.launch_options_clicked.connect(self.open_launch_options)
        self.client_page.show_clients_clicked.connect(lambda: self.open_settings('clients'))
        self.options_page = ClientOptionsPage()
        self.options_page.changed.connect(self.save_launch_options)
        self.options_page.closed.connect(self.show_client_page)
        self.settings_page = SettingsPage()
        self.settings_page.saved.connect(self.on_settings_saved)
        self.settings_page.recheck_catalog.connect(lambda: self.discover(force=True))
        self.settings_page.closed.connect(self.show_client_page)
        self.settings_page.choose_folder.connect(self.open_folder_picker)
        self.settings_page.copy_diagnostics.connect(self.collect_diagnostics)
        self.settings_page.move_folder.connect(self.open_move_picker)
        self.settings_page.start_at_sign_in.connect(self.set_start_at_sign_in)
        self.move_page = MovePage()
        self.move_page.cancelled.connect(lambda: self.stack.setCurrentWidget(self.settings_page))
        self.move_page.confirmed.connect(self.confirm_move)
        self.sign_in_page = SignInPage()
        self.sign_in_page.finished.connect(self.on_sign_in_finished)
        self.folder_page = FolderPage()
        self.folder_page.chosen.connect(self.on_folder_chosen)
        self.folder_page.cancelled.connect(lambda: self.stack.setCurrentWidget(self.settings_page))
        self.queue_page = QueuePage()
        self.queue_page.closed.connect(self.show_client_page)
        self.queue_page.pause.connect(lambda code: self.downloads.pause(code))
        self.queue_page.resume.connect(self.retry_or_resume)
        self.queue_page.cancel.connect(self.cancel_from_queue)
        self.queue_page.limit_changed.connect(self.set_limit)
        self.queue_page.move.connect(lambda code, offset: self.downloads.move(code, offset))
        self.queue_page.pause_all.connect(lambda: self.downloads.pause_all())
        self.queue_page.resume_all.connect(lambda: self.downloads.resume_all())
        for page in [self.client_page, self.settings_page, self.sign_in_page, self.folder_page, self.queue_page,
                     self.options_page, self.uninstall_page, self.import_page, self.import_folder_page, self.move_page]:
            self.stack.addWidget(page)
        right.addWidget(self.stack, 1)
        self.download_bar = DownloadBar()
        self.download_bar.pause_clicked.connect(lambda code: self.downloads.pause(code))
        self.download_bar.resume_clicked.connect(lambda code: self.downloads.enqueue(code, self.downloads.job(code).kind))
        self.download_bar.cancel_clicked.connect(lambda code: self.downloads.cancel(code))
        self.download_bar.open_client.connect(self.open_client)
        self.download_bar.limit_changed.connect(self.set_limit)
        self.download_bar.show_queue.connect(self.open_queue)
        self.download_bar.cancel_move.connect(lambda: self.downloads and self.downloads.cancel_move())
        self.download_bar.set_limit(self.settings.bandwidth_limit // MEGABYTE)
        right.addWidget(self.download_bar)
        layout.addLayout(right, 1)
        self.banner = self.client_page.banner
        self._tab_order: tuple = ()
        self._focus_primary = False
        self._update_key = None
        self.launch_failed: set[str] = set()     # clients whose last start failed, until dismissed
        self.messages: dict[str, tuple] = {}      # client, or '' for everything, -> banner arguments
        self.client_page.banner.dismissed.connect(self.dismiss_notice)
        self.tray = Tray(self)
        self.tray.show_window.connect(self.show_window)
        self.tray.pause_all.connect(lambda: self.downloads and self.downloads.pause_all())
        self.tray.resume_all.connect(lambda: self.downloads and self.downloads.resume_all())
        self.tray.play.connect(self.play_from_tray)
        self.tray.check_now.connect(self.check_now)
        self.tray.quit.connect(self.quit)
        self.notifier = Notifier(self.tray.icon)
        self.notifier.clicked.connect(self.reveal_client)
        self.notifier.acted.connect(self.on_notification_action)
        # While the window is hidden, a tray that went away, such as a panel that quit, brings it back.
        self.tray_check = QTimer(self)
        self.tray_check.timeout.connect(self.check_tray)
        self.tray_check.setInterval(5000)
        self._tray_grace = float('-inf')        # until then a start in the tray waits for the tray bar
        self._checking = False                  # an update check is under way
        self.update_timer = QTimer(self)
        self.update_timer.setTimerType(Qt.PreciseTimer)     # a coarse one runs late, and the tray says when

        self.populate()
        self.refresh()
        self.check_updates()
        self.discover()
        self.measure_storage()
        # Messages for every page, one at a time. The next shows once the current one is dismissed,
        # if it still applies then.
        self.startup_notices: list[tuple] = []
        if self.move_notice:
            self.startup_notices.append((lambda: True, *self.move_notice))
        if self.settings.problems:
            # First, since the file is rewritten without them on the next save.
            self.startup_notices.append((lambda: True, f'Some saved launch options were invalid and were '
                                         f'removed. {self.settings.problems[0]}', 'info'))
        if not self.library:
            self.startup_notices.append((
                lambda: not self.library, 'Choose an installation folder before installing a client.', 'info',
                'Choose folder', lambda: (self.open_settings(), self.open_folder_picker(str(Path.home())))))
        self.client_page.banner.dismissed.connect(self.next_startup_notice)
        self.next_startup_notice()
        timer = QTimer(self)
        timer.timeout.connect(self.refresh)
        timer.start(1000)
        self.update_timer.timeout.connect(self.check_updates)
        self.update_timer.timeout.connect(self.discover)
        self.update_timer.start(CHECK_INTERVAL * 1000)


    def open_library(self):
        if not self.settings.root:
            return
        if self.downloads:
            self.downloads.shutdown()
        self.library = Library(self.settings.root)
        self.downloads = DownloadManager(self.settings, self.library)
        self.downloads.listeners.append(lambda *args: post(self.bus.job_changed, *args))
        self.downloads.listeners.append(self.watch_run)
        self.downloads.move_listeners.append(lambda *args: post(self.bus.move_changed, *args))

    def recover_move(self) -> tuple | None:
        """Settle a move a crash interrupted. The worker deletes what is left of the other folder."""
        try:
            result = move.recover(self.settings, delete=False)
        except (LibraryError, OSError, ValueError) as error:
            return (f'An interrupted move of the installation folder could not be settled. {error}', 'error')
        if not result or result['recovered'] == 'nothing':
            return None
        if result['recovered'] == 'finished':
            return (f'The interrupted move of the installation folder was finished. It is now '
                    f'{result["destination"]}.', 'info')
        return (f'The interrupted move of the installation folder was undone. It stays at {result["source"]}.', 'info')

    def moving(self) -> MoveTask | None:
        task = self.downloads.moving if self.downloads else None
        return task if task and task.active else None

    # Layout

    def build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(280)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(SPACING, 18, SPACING, 12)
        side.setSpacing(0)
        brand = label('SCUFFLE', 'brand')
        brand.setContentsMargins(SPACING + 2, 0, 0, 6)
        side.addWidget(brand)
        self.client_list = ClientList()
        self.client_list.selected.connect(lambda _: self.show_client_page())
        self.client_list.open_settings.connect(lambda: self.open_settings('clients'))
        side.addWidget(self.client_list, 1)
        separator = QFrame()
        separator.setObjectName('separator')
        side.addWidget(separator)
        side.addSpacing(6)
        self.settings_button = fixed(QPushButton('Settings'))
        self.settings_button.setObjectName('nav')
        self.settings_button.setCheckable(True)
        self.settings_button.clicked.connect(self.toggle_settings)
        side.addWidget(self.settings_button)
        side.addSpacing(6)
        # Shown with several WoW accounts under the login. The choice saves the game's own question.
        self.game_account_row = QWidget()
        game_account = QHBoxLayout(self.game_account_row)
        game_account.setContentsMargins(SPACING + 2, 0, 0, 0)
        game_account.addWidget(label('WoW account', 'small'), 1)
        self.game_account_button = QPushButton('')
        self.game_account_button.setObjectName('flat')
        self.game_account_button.setToolTip('The WoW account the game starts with. With Ask in the game, '
                                            'the game shows its own choice.')
        self.game_account_button.clicked.connect(self.choose_game_account)
        game_account.addWidget(self.game_account_button)
        self.game_account_row.hide()
        side.addWidget(self.game_account_row)
        account = QHBoxLayout()
        account.setContentsMargins(SPACING + 2, 0, 0, 0)
        self.account = label('', 'small')
        # Signed in, it opens the account switcher. Signed out, it signs in.
        self.account_button = QPushButton('')
        self.account_button.setObjectName('flat')
        self.account_button.clicked.connect(self.account_action)
        account.addWidget(self.account, 1)
        account.addWidget(self.account_button)
        side.addLayout(account)
        return sidebar

    def kept_split(self) -> tuple[list[str], dict[str, str]]:
        """Installed clients, and clients with a download job by the state shown for it.

        A finished job stays in the download manager until the client's next one, but it
        no longer keeps the client listed.
        """
        installed = self.library.installed() if self.library else []
        names = {RUNNING: 'Downloading', QUEUED: 'Queued', PAUSED: 'Paused'}
        failed = {'install': 'Install failed', 'update': 'Update failed', 'verify': 'Needs repair',
                  'repair': 'Repair failed', 'uninstall': 'Uninstall failed'}
        return installed, {job.client: failed[job.kind] if job.status == FAILED else names.get(job.status, 'Downloading')
                           for job in (self.downloads.jobs if self.downloads else []) if job.status != DONE}

    def offered(self) -> list[catalog.Client]:
        """The clients Settings has a row for. Installed or downloading clients the catalog does not
        offer now, such as a product Blizzard stopped publishing, get one too, so they can be shown again."""
        clients = catalog.clients(self.settings.locale)
        codes = {c.code for c in clients}
        installed, downloading = self.kept_split()
        return clients + [catalog.get(code) for code in sorted(set(installed) | set(downloading))
                          if code not in codes and catalog.is_product(code)]

    def listed(self) -> list[catalog.Client]:
        """The clients the list shows, by the settings and what is installed or downloading."""
        installed, downloading = self.kept_split()
        s = self.settings
        return catalog.visible(catalog.clients(s.locale), extra=s.extra_clients, hidden=s.hidden_clients,
                               installed=installed, downloading=downloading, show_other=s.show_other_clients)

    def populate(self):
        clients = self.listed()
        key = [(c.code, c.label) for c in clients]
        if key == self._listed and len(key) == len(self.client_list.rows):
            return              # the same rows, so the list keeps its scroll position and focus
        self._listed = key
        self.client_list.populate(clients, self.client_list.current() or self.settings.selected)

    # State

    def selected(self) -> str | None:
        return self.client_list.current()

    def launcher(self) -> Launcher:
        return Launcher(self.settings, self.library)

    @staticmethod
    def vault():
        """The saved Battle.net logins. Reading them needs no bridge, since nothing is decrypted."""
        from ..auth.vault import Vault
        return Vault(data_dir() / 'session', None)

    def signed_in(self) -> bool:
        return self.vault().has_session()

    def active_login(self):
        return self.vault().active()

    def game_accounts(self) -> dict | None:
        """The WoW accounts of the active login, as its last sign-in or launch listed them."""
        login = self.active_login()
        return login.accounts() if login else None

    def show_game_account(self, signed: bool) -> None:
        from ..launch import chosen_account
        known = self.game_accounts() if signed else None
        several = bool(known and len(known['game_accounts']) > 1)
        self.game_account_row.setVisible(several)
        if several:
            chosen = chosen_account(self.settings, known)
            self.game_account_button.setText(f"{chosen['name'] if chosen else 'Ask in the game'} ▾")

    def choose_game_account(self) -> None:
        menu = self.game_account_menu()
        if menu is not None:
            menu.exec(self.game_account_button.mapToGlobal(QPoint(0, self.game_account_button.height())))

    def game_account_menu(self) -> QMenu | None:
        from ..launch import chosen_account
        known = self.game_accounts()
        if not known:
            return None
        chosen = chosen_account(self.settings, known)
        menu = QMenu(self.game_account_button)
        ask = menu.addAction('Ask in the game')
        ask.setCheckable(True)
        ask.setChecked(chosen is None)
        ask.triggered.connect(lambda: self.pick_game_account(known['owner'], None))
        menu.addSeparator()
        for account in known['game_accounts']:
            action = menu.addAction(f"{account['name']} · {account['region']}")
            action.setCheckable(True)
            action.setChecked(chosen is not None and account['id'] == chosen['id'])
            action.triggered.connect(lambda _=False, chosen=account['id']: self.pick_game_account(known['owner'], chosen))
        return menu

    def account_action(self) -> None:
        menu = self.account_menu()
        if menu is None:
            self.sign_in_from_sidebar()
        else:
            menu.exec(self.account_button.mapToGlobal(QPoint(0, self.account_button.height())))

    def account_menu(self) -> QMenu | None:
        """The Battle.net accounts with the active one checked, then adding one and signing out of it."""
        vault = self.vault()
        logins, active = vault.logins(), vault.active()
        if active is None:
            return None
        menu = QMenu(self.account_button)
        menu.setToolTipsVisible(True)
        for login in logins:
            action = menu.addAction(login.label)
            action.setCheckable(True)
            action.setChecked(login.key == active.key)
            action.triggered.connect(lambda _=False, key=login.key: self.switch_account(key))
        menu.addSeparator()
        add = menu.addAction('Add account…')
        if not self.can_sign_in():
            add.setEnabled(False)
            add.setToolTip('Install a client first. Signing in needs its version.')
        add.triggered.connect(self.sign_in_from_sidebar)
        # The account checked when the menu opened, even if another window switched meanwhile.
        menu.addAction('Sign out of this account').triggered.connect(lambda: self.sign_out_account(active))
        return menu

    def switch_account(self, key: str) -> None:
        try:
            self.vault().activate(key)
        except RuntimeError as error:
            self.notify(str(error), client='')
        self.refresh()

    def sign_out_account(self, login) -> None:
        try:
            self.vault().forget(login)
        except RuntimeError as error:
            self.notify(str(error), client='')
        self.refresh()

    def can_sign_in(self) -> bool:
        return bool(self.library and self.library.installed())

    def pick_game_account(self, owner: int, account: int | None) -> None:
        """Save the WoW account for this Battle.net login. None lets the game ask again."""
        if account is None:
            self.settings.game_accounts.pop(str(owner), None)
        else:
            self.settings.game_accounts[str(owner)] = account
        self.settings.save()
        self.refresh()

    def running_now(self) -> set[str]:
        """Clients whose game runs, including one Scuffle did not start.

        Reading every process takes tens of milliseconds, so a worker refreshes this at most
        every 2 s and the window shows the last result. Actions that need certainty check again.
        """
        if self.library and not self._scanning and time.monotonic() - self._running_at >= 2:
            self._scanning = True
            library = self.library

            def work():
                try:
                    found = self.scan_running(library)
                except Exception:   # a vanished folder or process; the next scan tries again
                    found = None
                post(self.bus.running_scanned, library, found)
            threading.Thread(target=work, name='scuffle-running', daemon=True).start()
        return self._running if self.library else set()

    @staticmethod
    def scan_running(library) -> set[str]:
        return set(library.running_games())

    def on_running_scanned(self, library, found) -> None:
        self._scanning = False
        self._running_at = time.monotonic()
        if library is self.library and found is not None and found != self._running:
            self._running = found
            self.refresh()
        if library is self.library and found is not None:
            # Every scan, so an update that found a game itself is released once the scan finds none.
            self.sync_game_downloads()

    def game_running(self, code: str) -> bool:
        """Running and past its start, so it can be closed."""
        return self.phases.get(code) == 'Running' or (code not in self.phases and code in self.running_now())

    def status(self, code: str) -> tuple[str, str]:
        """The short status of a client and its tone for the chip."""
        job = self.downloads.job(code) if self.downloads else None
        if self.game_running(code):
            # A running game outweighs a file check beside it.
            return ('Closing' if code in self.closing else 'Running'), 'active'
        if code in self.launch_failed and code not in self.phases and code in self.messages:
            return 'Did not start', 'danger'
        if job and job.status == RUNNING:
            verb = {'install': 'Installing', 'update': 'Updating', 'verify': 'Verifying',
                    'repair': 'Repairing', 'uninstall': 'Uninstalling'}[job.kind]
            verb = 'Importing' if job.seed_from else verb
            return (f'{verb} {job.fraction:.0%}' if job.total_bytes else verb), 'active'
        if job and job.status == QUEUED:
            return 'Queued', 'active'
        if job and job.status == PAUSED:
            if job.held and job.automatic:
                return 'Waiting for games', 'attention'
            if job.held:
                return 'Paused while playing', 'attention'
            return (f'Paused at {job.fraction:.0%}' if job.total_bytes else 'Paused'), 'attention'
        if job and job.status == FAILED:
            return {'install': 'Install failed', 'update': 'Update failed', 'verify': 'Needs repair',
                    'repair': 'Repair failed', 'uninstall': 'Uninstall failed'}[job.kind], 'danger'
        if code in self.phases:
            return self.phases[code], 'active'
        state = self.library.state(code) if self.library else None
        if not state:
            return 'Not installed', ''
        if self.updates.get(code):
            return 'Update available', 'attention'
        return 'Ready', 'good'

    def free_space(self) -> int:
        root = self.library.root
        return shutil.disk_usage(root if root.exists() else root.parent).free

    def facts(self, code: str) -> list[tuple[str, str]]:
        state = self.library.state(code) if self.library else None
        region = REGIONS.get((state or {}).get('region', self.settings.region), '')
        language = LOCALE_NAMES.get((state or {}).get('locale', self.settings.locale), '')
        if state:
            day = lambda t: datetime.fromtimestamp(t).strftime('%Y-%m-%d') if t else 'Never'
            facts = [('Version', state['version']), ('Size', size_text(state.get('encoded_bytes', 0))),
                     ('Updated', day(state.get('updated_at_unix', 0))),
                     ('Last played', day(self.settings.last_played.get(code, 0)))]
            # Region and language matter only where a client differs from the settings.
            if state.get('region', self.settings.region) != self.settings.region:
                facts.append(('Region', region))
            if state.get('locale', self.settings.locale) != self.settings.locale:
                facts.append(('Language', language))
            if isinstance(self.updates.get(code), str):
                facts.append(('New version', self.updates[code]))
            options = self.settings.client_launch.get(code)
            if options and options.active():
                facts.append(('Launch options', options_summary(options)))
            return facts
        job = self.downloads.job(code) if self.downloads else None
        if job and job.total_bytes:
            size = size_text(job.total_bytes)
        elif job and job.status == RUNNING:
            size = 'Being calculated'
        elif job and job.status == FAILED:
            size = 'Not known yet'
        else:
            size = 'Not known yet'
        published = catalog.release(code)
        version = [('Version', published.version)] if published else []
        return version + [('Region', region), ('Language', language), ('Download size', size)]

    def refresh(self):
        self.settle_waiters()
        self.running_now()          # also with no client listed, since automatic updates wait for any game
        for code, row in self.client_list.rows.items():
            text, _ = self.status(code)
            state = self.library.state(code) if self.library else None
            row.status.setText(f"Ready · {state['version']}" if text == 'Ready' else text)
        active = self.active_login()
        signed = active is not None
        signing_in = self.stack.currentWidget() is self.sign_in_page
        self.account.setText('Signing in…' if signing_in else
                             '<span style="color:#7fcf8f">●</span> Signed in' if signed else 'Not signed in')
        # The active account by its BattleTag or number, never by anything secret.
        self.account_button.setText(f'{active.label} ▾' if signed else 'Sign in')
        self.show_game_account(signed and not signing_in)
        can_sign_in = self.can_sign_in()
        # A second sign-in, or a sign-out, during a sign-in would race for the one ticket.
        # A launch uses the saved session, so it cannot be signed out of or switched mid-launch either.
        launching_any = any(phase != 'Running' for phase in self.phases.values())
        self.account_button.setEnabled((signed or can_sign_in) and not signing_in and not self._signing_in
                                       and not launching_any)
        self.account_button.setToolTip(
            'Available once the game has started' if launching_any
            else 'Available after the sign-in' if signing_in or self._signing_in
            else 'The Battle.net account Play uses. Switch, add or sign out here.' if signed
            else 'Install a client first. Signing in needs its version.' if not can_sign_in else '')
        self.refresh_downloads()
        self.refresh_tray()
        if self.stack.currentWidget() is self.settings_page:
            self.update_move_button()       # a game can start or stop at any time
        code = self.selected()
        if code is None:
            self.client_page.show_empty()
            self.update_tab_order()
            return
        self.refresh_client(code)
        if self.stack.currentWidget() is self.uninstall_page:
            self.uninstall_page.set_reason(self.uninstall_reason(self.uninstall_page.code))
        if self.stack.currentWidget() is self.import_page:
            self.import_page.set_blocked({c: r for c in self.import_codes() if (r := self.import_reason(c))})
        self.update_tab_order()

    def update_tab_order(self):
        """Tab follows the reading order: the list, the page, the sidebar footer, then the bar."""
        page = self.stack.currentWidget()
        chain = [self.client_list, self.client_list.empty_button,
                 *(page.focus_chain() if hasattr(page, 'focus_chain') else []),
                 self.settings_button, self.game_account_button, self.account_button, self.download_bar.title,
                 self.download_bar.toggle, self.download_bar.cancel, self.download_bar.queue]
        key = tuple(id(widget) for widget in chain)
        if key != self._tab_order:
            self._tab_order = key
            for first, second in zip(chain, chain[1:]):
                QWidget.setTabOrder(first, second)

    def refresh_client(self, code: str):
        signed = self.signed_in()
        self.settings.selected = code
        self.show_notice()
        client = catalog.get(code)
        state = self.library.state(code) if self.library else None
        job = self.downloads.job(code) if self.downloads else None
        status, tone_name = self.status(code)
        downloading = bool(job and job.status in {QUEUED, RUNNING, PAUSED})
        launching = code in self.phases
        failed = bool(job and job.status == FAILED)
        removing = bool(job and job.kind == 'uninstall' and job.status == RUNNING)
        moving = self.moving() is not None
        running = self.game_running(code)
        force = running and bool(QApplication.queryKeyboardModifiers() & Qt.ShiftModifier)
        if removing:
            primary = 'Uninstalling…'
        elif running:
            # Before a failed update's retry, since the running game is what failed it.
            primary = 'Force quit' if force else 'Closing…' if code in self.closing else 'Close game'
        elif failed and job.kind == 'verify':
            # A failed check is fixed by repairing, not by checking again.
            primary = 'Repair'
        elif failed:
            primary = 'Try again'
        elif downloading and job.status == QUEUED:
            # Waiting is not something to pause. Cancel takes it out of the queue.
            primary = 'Queued'
        elif downloading:
            primary = 'Resume' if job.status == PAUSED else 'Pause'
        elif launching:
            # The button names the same phase as the chip and the list.
            phase = self.phases[code]
            primary = 'Running' if phase == 'Running' else phase.split(' for ')[0].split(' · ')[0] + '…'
        elif not state:
            primary = 'Install'
        elif self.updates.get(code):
            primary = 'Update'
        else:
            # Play signs in first when there is no session, so the button says so.
            primary = 'Play' if signed else 'Sign in and play'
        progress, progress_text = None, ['']
        preparing = bool(downloading and job.status == RUNNING and not job.total_bytes)
        waiting = bool(job and job.status == PAUSED and job.held and job.automatic)
        if downloading:
            # The same details as the download bar, without the percentage the chip already shows.
            progress_text = [text.split(' · ', 1)[1] if text.startswith(f'{job.fraction:.0%} · ') else text
                             for text in progress_variants(job)]
            if job.total_bytes:
                progress = job.fraction
            # The job that really runs before this one: the current one, then waiting ones.
            # Paused jobs are skipped by the queue, so they are not named.
            line = [j for j in ([self.downloads.current()] + self.downloads.pending())
                    if j and (j is self.downloads.current() or j.status == QUEUED)]
            ahead = line[line.index(job) - 1] if job in line and line.index(job) > 0 else None
            if job.status == QUEUED and ahead:
                progress_text = [f'Starts after {catalog.get(ahead.client).label}', 'Waiting']
        hint = ''
        if not state and not job and self.library and self.library.installed():
            hint = 'Content the installed clients already have is not downloaded again.'
        if not state and not job:
            hint = (hint + ' Installed with Battle.net already? Import… in the ⋯ menu copies it from there.').strip()
        if client.test:
            hint = (hint + ' Test clients connect to Blizzard\'s test realms, which need account access.').strip()
        elif client.optional:
            hint = (hint + ' Other public clients are builds Blizzard publishes openly. '
                    'Many are old and may not connect.').strip()
        if catalog.stale(code):
            hint = (hint + f' This build is older than the live {catalog.get(client.parent).label} build.').strip()
        if self.library:
            clients = [(catalog.get(c), size) for c, size in self.storage.get('clients', {}).items()
                       if catalog.is_product(c)]
            self.client_page.storage.show_storage(str(self.library.root), self.storage.get('stored', 0),
                                                  clients, self.free_space(),
                                                  measuring=not self.storage and bool(self.library.installed()))
        self.client_page.storage.setVisible(bool(self.library))
        had_primary = self.client_page.primary.hasFocus()
        self.client_page.show_client(
            client, status=status, tone_name=tone_name, primary=primary,
            primary_enabled=bool(self.library) and not removing and primary != 'Queued'
                            and ((force or code not in self.closing) if running else not launching and not moving),
            primary_tip=self.primary_tip(code),
            installed=bool(state),
            busy=(launching and self.phases[code] != 'Running') or preparing, progress=progress,
            # An automatic update that waits for a game says so, as a queued one says what it waits for.
            busy_launch=launching, queued=bool(job and (job.status == QUEUED or waiting and not job.total_bytes)),
            progress_text=progress_text, cancellable=downloading and not removing, paused=bool(job and job.status == PAUSED),
            modes=[(m.id, m.label) for m in client.modes], mode=self.settings.modes.get(code),
            facts=self.facts(code), hint=hint, hideable=code not in self.kept_split()[1],
            # The notification's second choice, for a desktop whose notifications have no buttons.
            offer_always=primary == 'Update' and self.offers_automatic(code))
        if moving:
            self.client_page.verify_action.setEnabled(False)
        reason = self.uninstall_reason(code)
        self.client_page.uninstall_action.setEnabled(bool(state) and not reason)
        self.client_page.uninstall_action.setToolTip(reason)
        self.client_page.import_action.setEnabled(not self.import_reason(code))
        # Pause is a secondary action. Only starting something gets the accent color.
        secondary = primary in {'Pause', 'Queued'} or launching or running
        button = self.client_page.primary
        if button.property('secondary') != secondary:
            button.setProperty('secondary', secondary)
            button.style().unpolish(button)
            button.style().polish(button)
        # Focus on the main button survives it being disabled for a launch: the list holds it
        # meanwhile, so Enter never lands on the ⋯ menu, and it returns afterwards.
        focused = QApplication.focusWidget()
        if self._focus_primary and button.isEnabled() and focused in (None, self.client_list, self.client_page.more):
            self._focus_primary = False
            button.setFocus()
        elif not button.isEnabled() and (had_primary or focused is button):
            self._focus_primary = True
            self.client_list.setFocus()
        elif focused is not None and self.client_page.isAncestorOf(focused) and \
                not (focused.isVisible() and focused.isEnabled()):
            # A control that just hid itself hands focus to the main button, or the list.
            (button if button.isEnabled() else self.client_list).setFocus()

    def primary_tip(self, code: str) -> str:
        if not self.library:
            return 'Choose an installation folder in Settings first.'
        if self.moving() and not self.game_running(code):
            return 'Scuffle is moving the installation folder. Games and downloads start once it is done.'
        if self.game_running(code):
            return ('Closes the game as its window\'s close button does. Hold Shift to force quit, '
                    'which ends it at once, as when it hangs.')
        job = self.downloads.job(code) if self.downloads else None
        if job and job.status == PAUSED and job.held and job.automatic:
            return ('It starts by itself once every game in this folder has closed. Resume runs it now, '
                    'while a game runs.')
        options = self.settings.client_launch.get(code)
        if options and options.active() and self.library.state(code):
            return f'Starts with your launch options: {options_summary(options)}'
        return ''

    def align_download_bar(self):
        holder = getattr(self.stack.currentWidget(), 'column_holder', None)
        if holder is None or not holder.isVisible() or not self.download_bar.isVisible():
            return
        left = holder.mapTo(self, QPoint(0, 0)).x() - self.download_bar.mapTo(self, QPoint(0, 0)).x()
        self.download_bar.align_to(left, self.download_bar.width() - left - holder.width())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.align_download_bar)     # after the pages have their new widths

    def refresh_downloads(self):
        if not self.downloads:
            self.download_bar.hide()
            return
        if (task := self.moving()) is not None:
            # No job exists during a move, so the bar shows the move instead.
            self.download_bar.show_move(task)
            self.download_bar.setVisible(self.stack.currentWidget() is not self.queue_page)
            self.align_download_bar()
            return
        current = self.downloads.current()
        waiting = self.downloads.pending()
        jobs = ([current] if current else []) + waiting
        # The client page shows its own download, so the bar shows the next one instead.
        on_page = self.stack.currentWidget() is self.client_page
        shown = [j for j in jobs if not (on_page and j.client == self.selected())]
        job = shown[0] if shown else None
        failures = sum(1 for j in self.downloads.jobs if j.status == FAILED)
        if job:
            # The count matches the Downloads page, which also lists failures. With the page's own
            # download on screen, the bar keeps no buttons, so one Cancel never stops another client.
            mine = on_page and any(j.client == self.selected() for j in jobs)
            self.download_bar.show_job(job, catalog.get(job.client).label, len(jobs) - 1 + failures,
                                       controls=not mine)
        elif failures:
            # Failures wait on the Downloads page, so the way there stays.
            self.download_bar.show_summary(f'{failures} download{"s" if failures > 1 else ""} failed',
                                           'Try again or remove it on the Downloads page.', len(jobs) + failures)
        elif jobs:
            self.download_bar.show_summary('Downloads', 'This client\'s download is shown above.', len(jobs))
        listed = bool(jobs) or failures > 0
        self.download_bar.setVisible(listed and self.stack.currentWidget() is not self.queue_page)
        self.align_download_bar()
        failed = [j for j in self.downloads.jobs if j.status == FAILED]
        # A failure stays listed with its reason until it is retried or removed.
        jobs = jobs + failed
        self.queue_page.show_jobs(jobs, {j.client: catalog.get(j.client).label for j in jobs})
        if self.stack.currentWidget() is self.queue_page:
            self.update_tab_order()

    # Messages

    def notify(self, text: str, kind: str = 'error', action: str = '', callback=None, *, client: str | None = None):
        """Show a message on one client's page, or on every page when client is ''."""
        code = self.selected() if client is None else client
        self.messages[code or ''] = (text, kind, action, callback)
        if (self.unseen or code in self.from_shortcut) and not self._closing:
            self.present(code)
        self.show_notice()

    def next_startup_notice(self):
        while self.startup_notices and '' not in self.messages:
            applies, *notice = self.startup_notices.pop(0)
            if applies():
                self.notify(*notice, client='')

    def dismiss_notice(self):
        current = self.selected()
        self.messages.pop(current if current in self.messages else '', None)
        self.launch_failed.discard(current)
        # Dismissing a failed download also clears it from the list and brings back the usual
        # action. Damage found by a check stays until it is repaired.
        job = self.downloads.job(current) if self.downloads and current else None
        if job and job.status == FAILED and job.kind != 'verify':
            self.downloads.remove(current)
            # Its job may have been all that kept the client listed.
            self.populate()
            self.settings_page.lock_clients(*self.kept_split())
            self.refresh()

    def clear_notice(self, client: str | None = None):
        self.messages.pop(self.selected() if client is None else client, None)
        self.messages.pop('', None)
        self.show_notice()

    def show_notice(self):
        message = self.messages.get(self.selected()) or self.messages.get('')
        if message:
            self.client_page.banner.show_message(*message)
        else:
            self.client_page.banner.hide()

    # Navigation

    def show_client_page(self):
        self.settings_button.setChecked(False)
        self.client_list.set_active(self.stack.currentWidget() is not self.sign_in_page)
        if self.stack.currentWidget() is not self.sign_in_page:
            # An unsaved settings draft stays until it is saved or discarded.
            self.stack.setCurrentWidget(self.client_page)
        self.refresh()

    def open_client(self, code: str):
        if self.stack.currentWidget() is self.sign_in_page:
            return                  # the selection stays on the client being signed in for
        self.client_list.select(code)
        self.show_client_page()

    def toggle_settings(self):
        if self.stack.currentWidget() is self.settings_page:
            self.show_client_page()
        else:
            self.open_settings()

    def open_settings(self, section: str = ''):
        if self.stack.currentWidget() is self.sign_in_page:
            self.settings_button.setChecked(False)
            return
        self.settings_page.set_clients(self.offered())
        self.settings_page.show_catalog_state(self.catalog_state, self.catalog_checked_at())
        self.settings_page.set_tray_available(self.tray.available())
        self.settings_page.load(self.settings, *self.kept_split())
        self.settings_page.show_start_at_sign_in(shortcuts.starts_at_sign_in())
        if section:
            self.settings_page.show_section(section)
        self.settings_page.lock_folder(self.folder_lock())
        self.update_move_button()
        self.stack.setCurrentWidget(self.settings_page)
        self.settings_page.close_button.setFocus()  # so Esc closes it at once
        self.settings_button.setChecked(True)
        self.client_list.set_active(False)

    def open_launch_options(self):
        code = self.selected()
        if not code or self.stack.currentWidget() is self.sign_in_page:
            return
        self.options_page.load(catalog.get(code), self.settings.client_launch.get(code))
        self.stack.setCurrentWidget(self.options_page)
        self.options_page.close_button.setFocus()     # so Esc closes it at once
        self.client_list.set_active(False)
        self.refresh()

    def save_launch_options(self, code: str, options):
        if options.active():
            self.settings.client_launch[code] = options
        else:
            self.settings.client_launch.pop(code, None)
        self.settings.save()

    def open_folder_picker(self, folder: str):
        self.folder_page.start(folder)
        self.stack.setCurrentWidget(self.folder_page)

    def on_folder_chosen(self, folder: str):
        if self.folder_page.purpose == 'move':
            self.open_move_page(folder)
            return
        self.settings_page.set_root(folder)
        self.stack.setCurrentWidget(self.settings_page)

    # Moving the installation folder

    def move_reason(self) -> str:
        """Why the installation folder cannot move now, or nothing."""
        if self.moving():
            return 'The installation folder is being moved.'
        if self.phases or self.running_now():
            return 'Close the game before moving the installation folder.'
        if self.downloads and any(j.status != DONE for j in self.downloads.jobs):
            return 'Finish or remove every download before moving the installation folder.'
        return ''

    def update_move_button(self) -> None:
        self.settings_page.lock_move(self.move_reason(), bool(self.library and self.library.exists()))

    def open_move_picker(self):
        if self.move_reason() or not self.library:
            return
        self.folder_page.start(str(self.library.root.parent), purpose='move')
        self.stack.setCurrentWidget(self.folder_page)

    def open_move_page(self, folder: str):
        self.move_page.load(str(self.library.root), folder)
        self.stack.setCurrentWidget(self.move_page)
        self.update_tab_order()
        library = self.library

        def work():
            # Walking a large installation and reading free space can take a moment.
            post(self.bus.move_previewed, folder, move.preview(library, folder))

        threading.Thread(target=work, name='scuffle-move-check', daemon=True).start()

    def on_move_preview(self, folder: str, preview: dict):
        if folder == self.move_page.destination:
            self.move_page.show_preview(preview)
            reason = self.move_reason()
            if reason and not preview.get('problem'):
                self.move_page.set_reason(reason)

    def confirm_move(self):
        page = self.move_page
        reason = self.move_reason()
        if reason:
            page.set_reason(reason)
            return
        try:
            self.downloads.start_move(page.destination)
        except LibraryError as error:
            page.set_reason(str(error))
            return
        self.stack.setCurrentWidget(self.settings_page)
        self.update_tab_order()
        self.refresh()

    def on_move(self, task: MoveTask):
        if task.switched and self.library and not move.same_place(self.library.root, task.destination) \
                and not task.settle:
            # The setting names the new folder now. The page and the list read from it while
            # the old one is deleted, and the queue follows once the move is done.
            self.library = Library(task.destination)
            self.settings_page.show_root(self.settings.root)
        if not task.active:
            self.finish_move(task)
        self.settings_page.lock_folder(self.folder_lock())
        self.update_move_button()
        self.refresh()

    def finish_move(self, task: MoveTask):
        result, leftover = task.result, task.result.get('leftover')
        later = f' The old folder {leftover} could not be removed completely. Scuffle tries again on the next start.'
        if task.settle:
            if task.status == FAILED:
                self.notify(f'An interrupted move of the installation folder could not be finished. {task.error}',
                            'error', client='')
            elif leftover:
                self.notify(later.strip(), 'info', client='')
            return
        if task.status == FAILED:
            self.notify(f'The installation folder was not moved. {task.error}', 'error', client='')
        elif result.get('cancelled'):
            self.notify(f'The move was cancelled. The installation folder stays at {task.source}.', 'info', client='')
        elif result.get('moved'):
            self.settings_page.show_root(self.settings.root)
            self.open_library()
            self.measure_storage()
            self.check_updates()
            self.notify(f'The installation folder is now {task.destination}.' + (later if leftover else ''),
                        'info', client='')

    def open_queue(self):
        if self.stack.currentWidget() is self.sign_in_page:
            return                  # the sign-in finishes or is cancelled first
        self.queue_page.set_limit(self.settings.bandwidth_limit // MEGABYTE)
        self.settings_button.setChecked(False)
        self.client_list.set_active(False)
        self.stack.setCurrentWidget(self.queue_page)
        self.queue_page.close_button.setFocus()     # so Esc closes it at once
        self.refresh_downloads()

    # Actions

    def cancel_from_queue(self, code: str):
        job = self.downloads.job(code)
        if job and job.status == FAILED:
            self.clear_notice(code)         # removing a failure also removes its message
        self.downloads.cancel(code)
        self.refresh()

    def retry_or_resume(self, code: str):
        job = self.downloads.job(code)
        if job:
            self.clear_notice(code)
            self.downloads.enqueue(code, 'repair' if job.status == FAILED and job.kind == 'verify' else job.kind)

    def primary_action(self):
        code = self.selected()
        job = self.downloads.job(code)
        if self.game_running(code):
            self.close_game(code, force=bool(QApplication.queryKeyboardModifiers() & Qt.ShiftModifier))
            return
        if self.moving():
            return              # the button is disabled meanwhile, but a click can be on its way
        if job and job.status == FAILED:
            self.clear_notice(code)
            self.downloads.enqueue(code, 'repair' if job.kind == 'verify' else job.kind)
        elif job and job.status in {QUEUED, RUNNING}:
            self.downloads.pause(code)
        elif job and job.status == PAUSED:
            self.downloads.enqueue(code, job.kind)
        elif not self.library.state(code):
            self.downloads.enqueue(code, 'install')
        else:
            self.start(code)
        self.refresh()

    def start(self, code: str) -> None:
        """What the main button of an installed, idle client does: update it first, or play."""
        if self.updates.get(code):
            self.downloads.enqueue(code, 'update')
        else:
            self.play(code)

    def close_game(self, code: str, *, force: bool = False) -> None:
        executable, processes = self.library.executable(code), self.library.game_processes(code)
        if force:
            self.forced.add(code)       # its exit is not a crash
        else:
            # The grace time counts from the click, however long the request itself takes.
            asked = self.closing[code] = time.monotonic()
            QTimer.singleShot(CLOSE_GRACE * 1000, self, lambda: self.close_overdue(code, asked))
        self.refresh()

        def work():
            try:
                result = self.launcher().runtime.close(executable, processes, force=force)
            except Exception as error:  # shown in the banner
                result = str(error) or 'The game could not be closed.'
            post(self.bus.game_closed, code, result, force)

        threading.Thread(target=work, name='scuffle-close', daemon=True).start()

    def on_game_closed(self, code: str, result: str, force: bool) -> None:
        from ..runtime.game import ASKED, NO_WINDOW, NOT_RUNNING
        label = catalog.get(code).label
        if result != ASKED or force:
            self.closing.pop(code, None)   # an asked game quits in its own time, watched by close_overdue
        if result == NO_WINDOW:
            self.notify(f'{label} has no window to close. A game can hang like this while it quits. '
                        'Hold Shift and press Force quit to end it.', 'error', client=code)
        elif result not in (ASKED, NOT_RUNNING):
            self.notify(result, 'error', client=code)
        self._running_at = float('-inf')
        self.refresh()

    def close_overdue(self, code: str, asked: float) -> None:
        if self.closing.get(code) != asked:
            return
        self.closing.pop(code, None)
        self._running_at = float('-inf')
        if self.game_running(code):
            self.notify(f'{catalog.get(code).label} has not closed. Hold Shift and press Force quit to end it.',
                        'error', client=code)
        self.refresh()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease) and event.key() == Qt.Key_Shift:
            code = self.selected()
            if code and self.game_running(code):
                self.refresh_client(code)
        return super().eventFilter(watched, event)

    def verify(self):
        if self.moving():
            return
        self.downloads.enqueue(self.selected(), 'verify')
        self.refresh()

    def open_folder(self):
        folder = self.library.game_dir(self.selected())
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def hide_client(self):
        code = self.selected()
        # A download keeps its client listed, so there is nothing to hide until it is gone.
        if not code or code in self.kept_split()[1]:
            return
        self.settings.unlisted(code)
        self.settings.save()
        self.populate()
        name = catalog.get(code).label

        def undo():
            self.settings.listed(code)
            self.settings.save()
            self.populate()
            self.open_client(code)

        if self.selected():
            self.notify(f'{name} is hidden from the list. Settings can show it again.', 'info', 'Undo', undo,
                        client=self.selected())
        self.refresh()

    def uninstall_reason(self, code: str) -> str:
        if not self.library or not self.library.state(code):
            return 'This client is not installed.'
        if self.moving():
            return 'Wait until the installation folder has moved.'
        if code in self.phases:
            return 'Close the client before uninstalling.'
        job = self.downloads.job(code) if self.downloads else None
        if job and job.status != DONE:
            return 'Finish or remove this client\'s job before uninstalling.'
        if code in self.running_now():
            return 'Close the client before uninstalling.'    # the uninstall checks again before it deletes
        if self.library.pending(code) is not None:
            return 'Finish the unfinished update before uninstalling.'
        return ''

    def open_uninstall(self):
        code = self.selected()
        if not code or self.uninstall_reason(code):
            return
        state = self.library.state(code)
        size = self.storage.get('clients', {}).get(code, state.get('encoded_bytes', 0))
        self.uninstall_page.load(code, size)
        self.stack.setCurrentWidget(self.uninstall_page)
        self.update_tab_order()

    def confirm_uninstall(self):
        page = self.uninstall_page
        reason = self.uninstall_reason(page.code)
        if reason:
            page.set_reason(reason)
            return
        try:
            self.downloads.enqueue(page.code, 'uninstall', keep_settings=page.keep.isChecked())
        except LibraryError as error:
            page.set_reason(str(error))
            return
        self.show_client_page()
        self.refresh()

    # Importing

    def import_reason(self, code: str) -> str:
        """Why a client cannot be imported now, in a few words, or nothing."""
        if not self.library:
            return 'choose an installation folder first'
        if self.library.state(code):
            return 'installed'
        job = self.downloads.job(code) if self.downloads else None
        if job and job.status not in {DONE, FAILED}:
            return 'has a download'
        return ''

    def import_codes(self) -> set[str]:
        return {code for found in self.import_page.found for code in found.clients}

    def open_import(self):
        code = self.selected()
        if not code or self.import_reason(code) or self.stack.currentWidget() is self.sign_in_page:
            return
        self._import_search += 1
        number, exclude = self._import_search, self.library.root
        places = 'Program Files, Battle.net\'s records and local drives' if sys.platform == 'win32' else \
            'Wine prefixes, Steam, Lutris, ~/Games and mounted drives'
        self.import_page.start(code, f'Looking in {places}…')
        self._import_searching = True
        self.stack.setCurrentWidget(self.import_page)
        self.client_list.set_active(False)
        self.refresh()

        def work():
            search = imports.Search(exclude=exclude, cancelled=lambda: number != self._import_search)
            try:
                for found in search:
                    post(self.bus.import_found, number, found)
            except Exception:       # an unreadable place ends the search early. Choosing a folder still works
                search.complete = False
            post(self.bus.import_searched, number, search.complete)
        threading.Thread(target=work, name='scuffle-import-search', daemon=True).start()
        # A folder that hangs in the kernel, such as a stalled drive, cannot be interrupted.
        # The page stops waiting for it.
        QTimer.singleShot(int(imports.SEARCH_SECONDS * 1000) + 3000, self,
                          lambda: self.on_import_searched(number, False))

    def on_import_found(self, number: int, found) -> None:
        if number == self._import_search:
            self.import_page.add(found)
            self.refresh()

    def on_import_searched(self, number: int, complete: bool) -> None:
        if number != self._import_search or not self._import_searching:
            return
        self._import_searching = False
        page = self.import_page
        if complete:
            page.set_status('' if page.found else 'No installation found. Choose its folder if you have one.')
        else:
            page.set_status(f'The search stopped after {imports.SEARCH_SECONDS} seconds. '
                            'Choose the folder if yours is not listed.')

    def close_import(self):
        self._import_search += 1           # a running search stops at its next folder
        self._import_searching = False
        self.show_client_page()

    def choose_import_folder(self):
        # Home, not the found installation's folder, whose drive the window must not wait for.
        self.import_folder_page.start(str(Path.home()))
        self.stack.setCurrentWidget(self.import_folder_page)

    def on_import_folder_chosen(self, folder: str):
        self.stack.setCurrentWidget(self.import_page)
        self.import_page.set_reason('')
        self._import_search += 1
        self._import_searching = False
        number, exclude = self._import_search, self.library.root
        self.import_page.set_status('Reading the chosen folder…')

        def work():
            path = Path(folder)
            if not path.is_dir():
                post(self.bus.import_picked, number, [], 'This folder does not exist or cannot be read.')
                return
            if imports.overlaps(path, exclude):
                post(self.bus.import_picked, number, [], 'This folder is part of the Scuffle installation folder. '
                                                        'Choose an installation outside it.')
                return
            try:
                post(self.bus.import_picked, number, [imports.inspect(path, 'Chosen folder')], '')
                return
            except imports.NotImportable as error:
                reason = str(error)
            # A folder above the installation, such as a drive or Program Files.
            search = imports.Search([imports.Place(path, 'Chosen folder', 2)], seconds=5, exclude=exclude,
                                    cancelled=lambda: number != self._import_search)
            try:
                found = list(search)
            except Exception:
                found = []
            post(self.bus.import_picked, number, found, '' if found else reason)
        threading.Thread(target=work, name='scuffle-import-folder', daemon=True).start()

    def on_import_picked(self, number: int, found: list, reason: str) -> None:
        if number != self._import_search:
            return
        page = self.import_page
        page.set_status('')
        page.set_reason(reason)
        for index, item in enumerate(found):
            page.add(item, select=index == 0)
        self.refresh()

    def confirm_import(self):
        page = self.import_page
        found, codes = page.selected(), page.chosen_clients()
        if not found or not codes:
            page.set_reason('Choose an installation and at least one client.')
            return
        # The state can change while the page is open, such as a download that started meanwhile.
        blocked = [f'{catalog.get(c).label}: {r}' for c in codes if (r := self.import_reason(c))]
        if blocked:
            page.set_reason('Cannot import ' + '; '.join(blocked) + '.')
            self.refresh()
            return
        try:
            for code in codes:
                self.downloads.enqueue(code, 'install', seed_from=str(found.path),
                                       copy_settings=page.copy.isChecked())
        except LibraryError as error:
            page.set_reason(str(error))
            return
        self._import_search += 1
        self._import_searching = False
        # A hidden client is listed again by its download.
        self.populate()
        if page.code not in codes:
            self.client_list.select(codes[0])
        self.show_client_page()

    def remember_mode(self, mode: str):
        code = self.selected()
        if code and mode:
            self.settings.modes[code] = mode
            self.settings.save()

    def set_limit(self, megabytes: int):
        self.settings.bandwidth_limit = megabytes * MEGABYTE
        self.settings.save()
        if self.downloads:
            self.downloads.set_bandwidth_limit(self.settings.bandwidth_limit)

    def play(self, code: str, mode: str | None = None):
        client = catalog.get(code)
        mode = mode or self.settings.modes.get(code) or (client.modes[0].id if client.modes else None)
        self.clear_notice()
        self.launch_failed.discard(code)
        self.forced.discard(code)       # a Force quit of an earlier game says nothing about this one
        self.phases[code] = 'Checking for updates'
        self.refresh()

        def work():
            try:
                cdn = CDNClient(cache_dir(), self.library.state(code)['region'], code)
                if update_available(self.library, code, cdn):
                    post(self.bus.update_needed, code)
                    return
                process = self.launcher().play(code, self.request_sign_in, mode=mode,
                                               status=lambda text: post(self.bus.launch_phase, code, text))
                started = time.time()
                # Before Running, which can end a shortcut's process and save the settings.
                self.settings.last_played[code] = int(started)
                post(self.bus.launch_phase, code, 'Running')
                process.wait()
                post(self.bus.game_exited, code, process.returncode or 0, self.crash_report(code, started))
                post(self.bus.task_finished, code, '')
            except Exception as error:  # shown in the banner
                post(self.bus.task_finished, code, str(error))

        threading.Thread(target=work, name='scuffle-play', daemon=True).start()

    def request_sign_in(self, url: str) -> bytes:
        """Runs on a worker thread. Shows the sign-in page and waits for it."""
        self._sign_in_done.clear()
        post(self.bus.sign_in_requested, url)
        self._sign_in_done.wait()
        if not self._sign_in_result or self._sign_in_result[0] is None:
            raise LaunchError('Sign-in was cancelled')
        return self._sign_in_result[0]

    def on_sign_in_requested(self, url: str):
        self.present()          # also when a shortcut or the tray started the launch
        # Adding an account must not sign one in again. A sign-in for Play renews the same account.
        taken = {str(login.id): login.label for login in self.vault().logins()
                 if login.id is not None} if self._signing_in else {}
        self.sign_in_page.start(url, taken)
        self.settings_button.setChecked(False)
        self.stack.setCurrentWidget(self.sign_in_page)
        self.client_list.set_active(False)
        # The sign-in page finishes or is cancelled. Other pages wait until then.
        self.lock_navigation(True)
        self.sign_in_page.field.setFocus()
        self.refresh()

    def lock_navigation(self, locked: bool):
        """Ignore the sidebar without greying it out, so the selection cannot change underneath."""
        self.client_list.viewport().setAttribute(Qt.WA_TransparentForMouseEvents, locked)
        # Dimmed, so ignored clicks are expected rather than puzzling.
        if locked:
            effect = QGraphicsOpacityEffect(self.client_list)
            effect.setOpacity(0.55)
            self.client_list.setGraphicsEffect(effect)
        else:
            self.client_list.setGraphicsEffect(None)
        self.client_list.setFocusPolicy(Qt.NoFocus if locked else Qt.StrongFocus)
        self.settings_button.setEnabled(not locked)

    def on_sign_in_finished(self, ticket):
        self._sign_in_result = [ticket]
        self._sign_in_done.set()
        self.stack.setCurrentWidget(self.client_page)
        self.lock_navigation(False)
        self.client_list.set_active(True)
        self.refresh()

    def sign_in_from_sidebar(self):
        """Sign in to a Battle.net account through the browser. A second one is added and becomes active."""
        if not self.can_sign_in():
            return
        launcher = self.launcher()
        state = self.library.state(self.library.installed()[0])
        version = int(state['version'].rsplit('.', 1)[-1])
        self._signing_in = True
        self.refresh()

        def work():
            try:
                launcher.sign_in(version, self.request_sign_in)
                post(self.bus.task_finished, '', '')
            except Exception as error:
                post(self.bus.task_finished, '', str(error))

        threading.Thread(target=work, name='scuffle-sign-in', daemon=True).start()

    def on_phase(self, code: str, text: str):
        self.phases[code] = text
        if text == 'Running':
            self.from_shortcut.discard(code)
        self.sync_game_downloads()
        self.refresh()

    def sync_game_downloads(self):
        if self.downloads:
            self.downloads.set_game_running(self.settings.pause_while_playing and
                                            'Running' in self.phases.values())
            # A game Scuffle starts holds automatic updates from its first phase, before it runs.
            self.downloads.set_playing(bool(self.phases) or bool(self._running))

    def on_task_finished(self, code: str, message: str):
        self.phases.pop(code, None)
        self.closing.pop(code, None)
        self.forced.discard(code)
        self._running_at = float('-inf')        # the game is gone, so the list should not wait to say so
        self.sync_game_downloads()
        if not code:
            self._signing_in = False
        self.refresh()
        if not message or 'cancelled' in message.lower():
            self.from_shortcut.discard(code)
            return
        if code:
            # Seen in the list too, since another client may be selected by now.
            self.launch_failed.add(code)
            self.tell(code, f'{catalog.get(code).label} did not start', message)
        if code and not self.signed_in() and ('sign-in' in message.lower() or 'battle.net' in message.lower()):
            # The primary button now reads "Sign in and play", which is the way back.
            self.notify(message, 'error', client=code)
        elif code:
            self.notify(message, 'error', client=code)       # the main button below is the retry
        else:
            self.notify(message, client=code)
        self.from_shortcut.discard(code)
        self.refresh()

    def on_update_needed(self, code: str):
        self.phases.pop(code, None)
        self.updates[code] = True
        job = self.downloads.enqueue(code, 'update')
        where = 'It starts after the current download.' if job.status == QUEUED and self.downloads.busy() \
            else 'The update is running now.'
        self.notify(f'{catalog.get(code).label} needs an update before it can start. {where}', 'info', client=code)
        # A start from the tray has no window to show this in.
        self.tell(code, f'{catalog.get(code).label} needs an update', f'{where} Play again once it is done.')
        self.from_shortcut.discard(code)
        self.refresh()

    # Shortcuts and later starts

    def serve(self, window: instance.Window) -> None:
        """Answer later starts of Scuffle, such as a shortcut's, from this window."""
        window.serve(lambda request, reply: post(self.bus.request_received, request, reply))

    def on_request(self, request: dict, reply) -> None:
        if 'play' not in request:
            # Scuffle was started again, so its window comes to the front. A start at sign-in
            # finds it running already and leaves it where it is.
            if not request.get('tray'):
                self.present()
            reply.send(ok=True)
            reply.close()
            return
        code, mode, wait = request['play'], request.get('mode'), request.get('wait') is True
        problem = self.shortcut_problem(code, mode)
        if problem:
            reply.send(error=problem)
            reply.close()
            return
        # Kept by a waiting start, which watches the game itself if this window closes first.
        executable = self.library.executable(code) if self.library else None
        reply.send(ok=True, executable=str(executable) if executable else None)
        if not wait:
            reply.close()
        finished = (lambda: (reply.send(finished=True), reply.close())) if wait else None
        self.play_shortcut(code, mode, wait=wait, finished=finished)

    def start_shortcut(self, request: dict) -> None:
        """This process was started by a shortcut. It quits when done, unless the window was shown."""
        code, mode = request['play'], request.get('mode')
        problem = self.shortcut_problem(code, mode)
        if problem:
            self.notify(problem, 'error', client='')
            return
        self.play_shortcut(code, mode, wait=request.get('wait') is True, finished=self.leave)

    @staticmethod
    def shortcut_problem(code, mode) -> str:
        if not isinstance(code, str) or not catalog.is_product(code):
            return f'{code!r} is not a World of Warcraft client.'
        client = catalog.get(code)
        if mode is not None and mode not in [m.id for m in client.modes]:
            return f'{client.label} has no game mode {mode!r}.'
        return ''

    def play_shortcut(self, code: str, mode: str | None, *, wait: bool = False, finished=None) -> None:
        """Start a client as its Play button does. Anything the client needs first, such as an
        update or a sign-in, opens the window on it instead of starting the game.

        finished is called once there is nothing left to wait for: when the game has exited,
        or without wait, once it runs or could not start.
        """
        if finished:
            self.waiters.append((code, wait, finished))
        label = catalog.get(code).label
        job = self.downloads.job(code) if self.downloads else None
        if not self.library:
            self.present()                  # the notice asks for a folder
        elif code in self.phases:
            pass                            # already starting, and its waiter waits for it
        elif self.library.running(code):
            self._running = self._running | {code}      # before the scan sees it, so a waiter waits
        elif not self.library.state(code):
            self.notify(f'{label} is not installed. Install it to start it from a shortcut.', 'info', client=code)
        elif job and job.status == FAILED:
            self.present(code)              # its page shows the failure and the way to retry
        elif job and job.status != DONE:
            work = {'install': 'download', 'update': 'update', 'verify': 'file check', 'repair': 'repair'}
            self.notify(f'{label} can start once its {work.get(job.kind, "current job")} is done.', 'info', client=code)
        elif self.library.pending(code) is not None:
            self.notify(f'{label} has an unfinished update. Resume it before playing.', 'info', client=code)
        elif self.updates.get(code):
            self.from_shortcut.add(code)
            self.on_update_needed(code)
        else:
            self.from_shortcut.add(code)
            self.play(code, mode)
        self.refresh()

    def settle_waiters(self) -> None:
        for waiter in list(self.waiters):
            code, wait, finished = waiter
            started = self.phases.get(code) == 'Running'
            if (started and not wait) or (code not in self.phases and not self.game_running(code)):
                self.waiters.remove(waiter)
                finished()
        if self.leaving and self.unseen:
            QTimer.singleShot(0, self.quit_unseen)

    def leave(self) -> None:
        self.leaving = True

    def quit_unseen(self) -> None:
        """End a shortcut's process that never showed its window, once nothing else is starting."""
        launching = any(phase != 'Running' for phase in self.phases.values())
        if self.unseen and not self._closing and not self.waiters and not launching:
            self.quit()         # the full close, which with the tray setting on would only hide

    def present(self, code: str | None = None) -> None:
        """Bring the window to the front, on code's page when given."""
        self.unseen = False
        if code:
            if code not in self.client_list.rows:
                self.settings.listed(code)      # its shortcut brings a hidden client back
                self.settings.save()
                self.populate()
            self.open_client(code)
        self.show_window()

    def launcher_place(self) -> str:
        return 'Start menu' if system.WINDOWS else 'app launcher'

    def update_shortcut_actions(self) -> None:
        """Say in the menu whether the client has an entry, as the menu opens."""
        code = self.selected()
        page = self.client_page
        installed = bool(code and self.library and self.library.state(code))
        listed = installed and shortcuts.in_launcher(code)
        page.launcher_action.setVisible(installed)
        page.launcher_action.setText(f'{"Remove from" if listed else "Add to"} {self.launcher_place()}')
        found = steam.find() if installed else None
        page.steam_action.setVisible(found is not None)
        page.steam_action.setText('Remove from Steam' if found and found.has(code) else 'Add to Steam')

    def update_auto_action(self) -> None:
        """The client's own choice about automatic updates, as the menu opens.

        A live client follows the switch in Settings, so it offers only its exception while the
        switch is on. A test or other client is turned on by itself.
        """
        code = self.selected()
        action = self.client_page.auto_action
        installed = bool(code and self.library and self.library.state(code))
        action.setVisible(installed and (catalog.get(code).optional or self.settings.auto_update))
        if installed:
            action.setText("Don't update automatically" if self.settings.updates_automatically(code)
                           else 'Update automatically')

    def toggle_auto_update(self) -> None:
        code = self.selected()
        if not code:
            return
        on = not self.settings.updates_automatically(code)
        self.settings.set_updates_automatically(code, on)
        self.settings.save()
        label = catalog.get(code).label
        if on:
            self.update_automatically()
            self.notify(f'{label} updates automatically, once every game in the installation folder has closed.',
                        'info', client=code)
        else:
            self.stop_automatic_updates()
            self.notify(f'{label} no longer updates automatically. Its updates wait for you.', 'info', client=code)
        self.refresh()

    def set_start_at_sign_in(self, on: bool) -> None:
        try:
            shortcuts.start_at_sign_in(on)
            problem = ''
        except OSError as error:
            problem = f'Scuffle could not change how it starts. {error}'
        self.settings_page.show_start_at_sign_in(shortcuts.starts_at_sign_in(), problem)

    def toggle_launcher(self) -> None:
        code = self.selected()
        if not code:
            return
        client, place = catalog.get(code), self.launcher_place()
        try:
            if shortcuts.in_launcher(code):
                shortcuts.remove_from_launcher(code)
                self.notify(f'{client.label} was removed from the {place}.', 'info', client=code)
            else:
                shortcuts.add_to_launcher(client, {size: png(tile_image(client, size))
                                                   for size in shortcuts.ICON_SIZES})
                self.notify(f'{client.label} is in the {place} now. It opens Scuffle only when the game '
                            'needs a sign-in or an update first.', 'info', client=code)
        except OSError as error:
            self.notify(f'The {place} entry could not be changed. {error}', 'error', client=code)

    def toggle_steam(self) -> None:
        code = self.selected()
        found = steam.find()
        if not code or found is None:
            return
        client = catalog.get(code)
        try:
            if found.has(code):
                found.remove(code)
                self.notify(f'{client.label} was removed from Steam.', 'info', client=code)
            else:
                images = {'portrait': png(capsule_image(client, 600, 900)),
                          'wide': png(capsule_image(client, 920, 430)), 'icon': png(tile_image(client, 256))}
                found.add(code, client.label, shortcuts.shortcut_command(code, wait=True), images)
                self.notify(f'{client.label} was added to Steam. It appears in your library when Steam starts.',
                            'info', client=code)
        except steam.SteamRunning:
            # Steam writes its own list back while it runs, which would drop the change.
            self.notify('Steam is running and would undo the change. Quit Steam, then try again.', 'info',
                        'Try again', self.toggle_steam, client=code)
        except (OSError, steam.VdfError) as error:
            self.notify(f'Steam\'s shortcuts were left unchanged. {error}', 'error', client=code)

    def measure_storage(self):
        if not self.library:
            return
        library = self.library

        def work():
            try:
                post(self.bus.storage_ready, library.storage())
            except Exception:
                pass

        threading.Thread(target=work, name='scuffle-storage', daemon=True).start()

    def on_storage(self, storage: dict):
        self.storage = storage
        self.refresh()

    def folder_lock(self) -> str:
        """Why the installation folder cannot change now, or nothing."""
        if self.moving():
            return 'The installation folder is being moved.'
        if self.downloads and (self.downloads.busy() or self.downloads.pending()):
            return 'Finish or cancel the downloads to choose another folder.'
        return ''

    def on_job(self, job: Job):
        self.settings_page.lock_folder(self.folder_lock())
        self.update_move_button()
        self.settings_page.set_clients(self.offered())
        self.settings_page.lock_clients(*self.kept_split())
        if job.status == DONE:
            self.clear_notice(job.client)      # an "update needed" notice is answered now
        # A new job can bring a hidden client into the list. A finished or removed one can
        # take it out again.
        self.populate()
        if job.status == DONE and job.kind in {'install', 'update', 'uninstall'}:
            self.updates[job.client] = False
            self.measure_storage()
        if job.status == DONE and job.kind == 'uninstall':
            message = f'{catalog.get(job.client).label} uninstalled.'
            try:
                # Its entry would only open the window now. Steam is left alone, since it may be running.
                shortcuts.remove_from_launcher(job.client)
            except OSError as error:
                message += f' Its app launcher entry could not be removed: {error}'
            if job.result.get('backup'):
                message += f" Addons and settings saved in {Path(job.result['backup']).name}."
            if job.result.get('cleanup', {}).get('deferred'):
                message += ' Shared content cleanup will finish after other clients close or finish updating.'
            self.notify(message, 'info', client=job.client if job.client in self.client_list.rows else '')
        if job.status == DONE and job.kind == 'install' and 'settings' in job.result:
            self.notify(self.settings_copied(job.result['settings']), 'info', client=job.client)
        if job.status == FAILED:
            # The message shows on that client's own page, so it needs no name.
            # Dismiss also clears a failed download. Found damage stays until it is repaired.
            self.notify(job.error, 'error' if job.kind == 'verify' else 'failure', client=job.client)
        self.refresh()

    @staticmethod
    def settings_copied(result: dict) -> str:
        """What copying an imported client's settings and addons did."""
        if result.get('error') and not result.get('copied'):
            return f"Imported. Settings and addons were not copied: {result['error']}"
        text = 'Imported, with your settings and addons.' if result.get('copied') else \
            'Imported. The installation had no settings or addons to copy.'
        if result.get('kept'):
            count = result['kept']
            text += f" {count} {'file' if count == 1 else 'files'} already here {'was' if count == 1 else 'were'} kept."
        if result.get('failed'):
            text += f" {result['failed']} could not be copied: {result['error']}"
        return text

    def check_updates(self):
        self._update_key = (self.settings.root, self.settings.region, self.settings.locale)
        if not self.library:
            return
        library = self.library
        self._checking = True

        def work():
            found = {}
            for code in library.installed():
                try:
                    cdn = CDNClient(cache_dir(), library.state(code)['region'], code)
                    if update_available(library, code, cdn):
                        found[code] = cdn.version['VersionsName']
                    else:
                        found[code] = False
                except Exception:
                    continue
            post(self.bus.updates_checked, found)

        threading.Thread(target=work, name='scuffle-updates', daemon=True).start()

    def on_updates(self, found: dict):
        self._checking = False
        self.updates.update(found)
        # First, so a client that updates by itself is not announced as waiting for the user.
        self.update_automatically()
        self.announce_updates(found)
        self.refresh()

    def check_now(self):
        """Check at once, from the tray. The next regular check is a full interval later."""
        if self._checking:
            return
        self.update_timer.start(CHECK_INTERVAL * 1000)
        self.check_updates()
        self.discover()
        self.refresh()

    def update_automatically(self):
        """Queue an update for each client that is behind. The queue checks the user's consent,
        any job the client has and a recent failure of the same update."""
        if not self.downloads:
            return
        for code, behind in list(self.updates.items()):
            if not behind or not self.library.state(code):
                continue
            # True is a start's own check, which names no version.
            version = behind if isinstance(behind, str) else ''
            previous = self.downloads.job(code)
            if self.downloads.enqueue_automatic(code, version) and previous and previous.status == FAILED:
                self.messages.pop(code, None)       # the failure's message, now that it is tried again

    def stop_automatic_updates(self):
        """Updates queued by consent the user has withdrawn wait for the user, paused."""
        for job in list(self.downloads.jobs if self.downloads else []):
            if job.automatic and job.status in {QUEUED, RUNNING, PAUSED} and \
                    not self.settings.updates_automatically(job.client):
                self.downloads.pause(job.client)

    def offers_automatic(self, code: str) -> bool:
        """Whether asking to always update automatically means something new for this client.

        A live client the user excepted while the switch is on is not asked again.
        """
        if catalog.get(code).optional:
            return code not in self.settings.auto_update_extra
        return not self.settings.auto_update

    def always_update(self, code: str):
        """Update this client by itself from now on, the live ones all together, and start with this update."""
        self.settings.set_updates_automatically(code, True)
        self.settings.save()
        if self.stack.currentWidget() is self.settings_page:
            self.settings_page.load(self.settings, *self.kept_split())
        self.update_automatically()
        self.refresh()

    def update_now(self, code: str):
        """Update one client once, as its Update button does."""
        job = self.downloads.job(code) if self.downloads else None
        if self.library and self.library.state(code) and not self.moving() and not (job and job.status != DONE):
            self.downloads.enqueue(code, 'update')
            self.refresh()

    def on_notification_action(self, code: str, action: str):
        if action == 'update':
            self.update_now(code)
        elif action == 'always':
            self.always_update(code)

    def discover(self, force: bool = False):
        """Ask the version service for the current clients, when the cached answer is old."""
        region = self.settings.region
        snapshot = discovery.load(cache_dir(), region)
        if region in self._discovering or (discovery.fresh(snapshot) and not force):
            return
        if force:
            self.catalog_state = 'checking'
            self.settings_page.show_catalog_state('checking')
        self._discovering.add(region)
        cache = cache_dir()

        def work():
            try:
                result = discovery.refresh(region, cache, snapshot)
                discovery.save(cache, result)
            except Exception:       # offline, or the service changed. The last list stays.
                result = None
            post(self.bus.catalog_checked, region, result)

        threading.Thread(target=work, name='scuffle-catalog', daemon=True).start()

    def on_catalog(self, region: str, snapshot):
        self._discovering.discard(region)
        if region != self.settings.region:
            self.discover()         # the region changed while this one was checked
            return
        if snapshot:
            table = battlenet.current(cache_dir())
            catalog.apply(snapshot['products'], table['installs'], table.get('dropped'))
        # A failed check with an earlier answer shows that answer, and says it is old.
        self.catalog_state = ('ready' if snapshot else 'stale') if catalog.checked() else 'offline'
        self.show_catalog()

    def catalog_checked_at(self) -> int:
        snapshot = discovery.load(cache_dir(), self.settings.region)
        return snapshot['checked_at'] if snapshot else 0

    def show_catalog(self):
        self.settings_page.set_clients(self.offered())
        self.settings_page.show_catalog_state(self.catalog_state, self.catalog_checked_at())
        self.populate()
        self.refresh()

    def on_settings_saved(self):
        self.sync_game_downloads()
        # The switch for automatic updates may have changed either way.
        self.stop_automatic_updates()
        self.update_automatically()
        if self.downloads:
            self.downloads.set_bandwidth_limit(self.settings.bandwidth_limit)
        self.download_bar.set_limit(self.settings.bandwidth_limit // MEGABYTE)
        changed = self.settings.root and (not self.library or Path(self.settings.root).resolve() != self.library.root)
        # The folder is locked while downloads exist, so a change always applies at once.
        if changed and not self.folder_lock():
            self.open_library()
            self.measure_storage()
            self.clear_notice('')
        if self.settings.region != self._catalog_region:
            # Versions differ by region. The cached list for it shows until it is checked.
            self._catalog_region = self.settings.region
            discovery.restore(cache_dir(), self.settings.region)
            self.catalog_state = 'ready' if catalog.checked() else 'checking'
            self.discover()
        self.show_catalog()
        # Every typing pause saves. Only a new folder, region or language changes what is current.
        key = (self.settings.root, self.settings.region, self.settings.locale)
        if key != self._update_key:
            self.check_updates()

    def collect_diagnostics(self):
        """Build the report on a worker, since it asks umu-run for its version and reads the library."""
        # Copies, so the worker reads a fixed picture while the window and the queue carry on.
        settings, updates = copy.deepcopy(self.settings), dict(self.updates)
        jobs = [replace(job) for job in self.downloads.jobs] if self.downloads else []
        library, platform = self.library, QApplication.platformName()

        def work():
            try:
                text = diagnostics.report(settings, library, jobs, updates=updates, qt_platform=platform)
            except Exception:       # report() collects each part on its own. This is the last guard.
                text = ''
            post(self.bus.diagnostics_ready, text)

        threading.Thread(target=work, name='scuffle-diagnostics', daemon=True).start()

    def on_diagnostics(self, text: str):
        if text:
            QApplication.clipboard().setText(text)
        self.settings_page.diagnostics_copied(bool(text))

    def closeEvent(self, event):
        if not self._quitting and self.settings.keep_in_tray and self.tray.available():
            # Downloads and games carry on. The tray icon brings the window back, or quits.
            event.ignore()
            self.hide_to_tray()
            return
        self._closing = True
        # Every event in the application passes the filter, so it goes with the window.
        QApplication.instance().removeEventFilter(self)
        if self.downloads:
            self.downloads.shutdown()
        if self.downloads and self.downloads.busy():
            # Keep stored content and the queue for the next start. A move stops before its switch,
            # or the next start finishes it.
            self.notify('Stopping the move before closing…' if self.moving() else 'Pausing downloads before closing…',
                        'info', client='')
            self.stack.setCurrentWidget(self.client_page)
            QApplication.processEvents()
            deadline = time.monotonic() + 30
            while self.downloads.busy() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.05)
        self.settings.save()
        self.tray_check.stop()
        self.tray.hide()
        self.notifier.close()
        event.accept()

    def quit(self):
        """Quit for good, as closing the window does without the tray. Also from a hidden window."""
        self._quitting = True
        if self.close():
            QApplication.quit()     # the window may have been hidden already, so no last window closes

    def hide_to_tray(self):
        self.settings.save()
        # The hidden window is no last window to close, and a menu closing must not quit either.
        QApplication.setQuitOnLastWindowClosed(False)
        self._in_tray = True
        self.hide()
        self.tray_check.start()

    def show_window(self):
        QApplication.setQuitOnLastWindowClosed(True)
        self._in_tray = False
        self.tray_check.stop()
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    def check_tray(self):
        # A start at sign-in can come before the tray bar, which gets a moment to appear.
        if self._in_tray and not self.tray.available() and time.monotonic() >= self._tray_grace:
            self.show_window()      # never a process the user cannot reach

    def start_in_tray(self):
        """Started at sign-in: hidden in the tray, or shown once it is clear the desktop has none."""
        self._tray_grace = time.monotonic() + TRAY_GRACE
        self.hide_to_tray()

    # The tray and desktop notifications

    def refresh_tray(self):
        jobs = self.downloads.jobs if self.downloads else []
        self.tray.show_state(self.downloads_line(),
                             can_pause=any(j.status in {QUEUED, RUNNING} for j in jobs),
                             can_resume=any(j.status == PAUSED for j in jobs),
                             clients=self.tray_clients(), next_check=self.next_check_line(),
                             can_check=not self._checking)

    def next_check_line(self) -> str:
        """When the update check runs next, for the tray. Nothing without an installed client."""
        if not self.library or not self.library.installed():
            return ''
        if self._checking:
            return 'Checking for updates…'
        minutes = max(1, -(-self.update_timer.remainingTime() // 60_000))
        return f'Next check in {minutes} min'

    def downloads_line(self) -> str:
        """The state of the downloads in a few words, for the tray."""
        current = self.downloads.current() if self.downloads else None
        jobs = self.downloads.jobs if self.downloads else []
        if current:
            verb = {'install': 'Installing', 'update': 'Updating', 'verify': 'Verifying',
                    'repair': 'Repairing', 'uninstall': 'Uninstalling'}[current.kind]
            line = f'{verb} {catalog.get(current.client).label}'
            return f'{line} · {current.fraction:.0%}' if current.total_bytes else line
        counts = [(sum(1 for j in jobs if j.status == status), word) for status, word in
                  [(QUEUED, 'waiting'), (PAUSED, 'paused'), (FAILED, 'failed')]]
        parts = [f'{count} {word}' for count, word in counts if count]
        total = sum(count for count, _ in counts)
        return f'Downloads: {", ".join(parts)}' if total else 'No downloads'

    def start_label(self, code: str) -> str:
        """What the main button starts for an installed client that is idle, or '' when it starts nothing."""
        job = self.downloads.job(code) if self.downloads else None
        if not self.library or not self.library.state(code) or (job and job.status != DONE) \
                or code in self.phases or self.game_running(code):
            return ''
        if self.stack.currentWidget() is self.sign_in_page:
            return ''           # the window locks every other client during a sign-in too
        return 'Update' if self.updates.get(code) else 'Play' if self.signed_in() else 'Sign in and play'

    def tray_clients(self) -> list[tuple[str, str, bool]]:
        installed = set(self.library.installed()) if self.library else set()
        codes = [c.code for c in self.listed() if c.code in installed]
        codes.sort(key=lambda code: -self.settings.last_played.get(code, 0))
        clients = []
        for code in codes[:PLAY_LIMIT]:
            label, action = catalog.get(code).label, self.start_label(code)
            clients.append((code, f'{action} {label}' if action else f'{label} · {self.status(code)[0]}',
                            bool(action)))
        return clients

    def play_from_tray(self, code: str):
        """The same as the main button, checked again since the menu was filled."""
        if self.start_label(code):
            self.start(code)
        self.refresh()

    def looking_at(self, code: str, *, downloads: bool = False) -> bool:
        """Whether the window shows this client to the user right now, so a notification would repeat it."""
        if not self.isVisible() or self.isMinimized() or not self.isActiveWindow():
            return False
        page = self.stack.currentWidget()
        return (page is self.client_page and self.selected() == code) or (downloads and page is self.queue_page)

    def tell(self, code: str, title: str, body: str, *, downloads: bool = False, category: str = '',
             actions: tuple = ()):
        """A desktop notification about a client, unless they are off or the window shows it already."""
        if self.settings.show_notifications and not self.looking_at(code, downloads=downloads):
            self.notifier.show(title, body, code, category, actions)

    def reveal_client(self, code: str):
        """A click on a notification opens the window on its client."""
        self.show_window()
        if code in self.client_list.rows:
            self.open_client(code)

    def watch_run(self, job: Job):
        """Runs on the thread that changed the job. Only a run's end counts, not a failure removed later."""
        before = self._runs.get(job.client)
        status = job.status
        self._runs[job.client] = (job, status)
        if status in (DONE, FAILED) and before and before[0] is job and before[1] == RUNNING:
            post(self.bus.download_finished, job, status)

    def on_download_finished(self, job: Job, status: str):
        if job.kind not in {'install', 'update', 'repair'}:
            return
        label = catalog.get(job.client).label
        if status == DONE and job.automatic:
            state = self.library.state(job.client) if self.library else None
            version = state['version'] if state else job.version
            self.tell(job.client, f'{label} updated to {version}', 'Updated automatically. Ready to play.',
                      downloads=True, category='transfer.complete')
        elif status == DONE:
            state = self.library.state(job.client) if self.library else None
            title = {'install': f'{label} is installed', 'update': f'{label} is up to date',
                     'repair': f'{label} is repaired'}[job.kind]
            body = f"Version {state['version']} is ready to play." if state else 'Ready to play.'
            self.tell(job.client, title, body, downloads=True, category='transfer.complete')
        else:
            title = {'install': f'{label} did not install', 'update': f'{label} did not update',
                     'repair': f'{label} was not repaired'}[job.kind]
            self.tell(job.client, title, job.error or 'Try again in Scuffle.', downloads=True,
                      category='transfer.error')

    def announce_updates(self, found: dict):
        """One notification per new version of a client, also across restarts."""
        announced = self.settings.announced_updates
        new = {code: version for code, version in found.items()
               if isinstance(version, str) and announced.get(code) != version}
        for code, version in new.items():
            job = self.downloads.job(code) if self.downloads else None
            # An update on its way says it already, and so does one the user lets run by itself.
            if not (job and job.status != DONE) and not self.settings.updates_automatically(code):
                # The consent asked for where it matters. The body still opens the client.
                actions = (('update', 'Update now'),) + \
                    ((('always', 'Always update automatically'),) if self.offers_automatic(code) else ())
                self.tell(code, f'Update for {catalog.get(code).label}', f'Version {version} is available.',
                          actions=actions)
            announced[code] = version
        if new:
            self.settings.save()

    def crash_report(self, code: str, started: float) -> bool:
        """Whether the game wrote a crash report since it started. It runs on the play thread."""
        folder = self.library.game_dir(code)
        try:
            # File times can lag the clock by a tick, so a report written at once still counts.
            return any(entry.is_file() and entry.stat().st_mtime >= started - 2
                       for entry in (folder / 'Errors').iterdir()) if folder else False
        except OSError:
            return False

    def on_game_exited(self, code: str, exit_code: int, crash_report: bool):
        if code in self.forced or not (exit_code or crash_report):
            return              # a force quit is not a crash, and code 0 is a game that quit itself
        detail = ('Its crash report is in the Errors folder of its game folder.' if crash_report else
                  f'It ended with signal {-exit_code}.' if exit_code < 0 else
                  f'It exited with code {exit_code:#x}.' if exit_code > 255 else f'It exited with code {exit_code}.')
        self.tell(code, f'{catalog.get(code).label} quit unexpectedly', detail)


def prepare(application: QApplication) -> None:
    """A fixed style, palette and stylesheet, so desktop themes such as Breeze do not mix in."""
    application.setStyle(QStyleFactory.create('Fusion'))
    application.setPalette(dark_palette())
    application.setStyleSheet(stylesheet(make_images(cache_dir() / 'ui')))


def close_on_signals(window: MainWindow) -> None:
    """Ctrl+C or a terminate request closes the window as its close button would without the tray.

    Qt swallows the KeyboardInterrupt Python raises inside its callbacks, so without this
    the terminal cannot stop the launcher. A second Ctrl+C quits at once.
    """
    def close(number, frame):
        signal.signal(number, signal.SIG_DFL)
        # Quits even when closing would keep Scuffle in the tray. Outside whatever callback the
        # signal interrupted.
        QTimer.singleShot(0, window.quit)
    signal.signal(signal.SIGINT, close)
    signal.signal(signal.SIGTERM, close)


def main(request: dict | None = None, *, tray: bool = False):
    """Show the window, or with request run a shortcut's start, which shows it only when needed.
    With tray the window starts hidden in the tray, as at sign-in.

    When Scuffle already runs, the request goes to its window and this process ends.
    """
    # Qt Wayland reports harmless text-input focus mismatches on every focus change.
    os.environ.setdefault('QT_LOGGING_RULES', 'qt.qpa.wayland.textinput=false')
    # Scuffle sets its own style. A desktop override such as Breeze only prints a warning.
    os.environ.pop('QT_STYLE_OVERRIDE', None)
    owner = instance.claim(request or ({'tray': True} if tray else {'show': True}))
    if owner is None:
        return
    application = QApplication(sys.argv)
    application.setApplicationName('Scuffle')
    application.setDesktopFileName('scuffle')       # Wayland matches the window to scuffle.desktop
    application.setWindowIcon(QIcon(str(Path(__file__).with_name('icon.svg'))))
    prepare(application)
    window = MainWindow(unseen=request is not None)
    window.serve(owner)
    close_on_signals(window)
    if request:
        window.start_shortcut(request)
    elif tray:
        window.start_in_tray()
    else:
        window.show()
    code = application.exec()
    owner.close()
    sys.exit(code)
