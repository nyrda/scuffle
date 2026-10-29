"""The Waystone window."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QMainWindow, QPushButton, QStackedWidget,
                               QMenu, QStyleFactory, QVBoxLayout, QWidget)

from .. import catalog, discovery, system
from ..downloads import DONE, FAILED, PAUSED, QUEUED, RUNNING, DownloadManager, Job
from ..launch import Launcher, LaunchError
from ..library import Library, LibraryError, update_available
from ..ngdp.cdn import CDNClient
from ..settings import LOCALE_NAMES, MEGABYTE, REGIONS, Settings, cache_dir, data_dir
from .client_options import ClientOptionsPage, summary as options_summary
from .pages import ClientPage, FolderPage, QueuePage, SettingsPage, SignInPage, UninstallPage
from .style import SPACING, dark_palette, make_images, size_text, stylesheet
from .widgets import ClientList, DownloadBar, fixed, label, progress_variants


CLOSE_GRACE = 20      # seconds a game gets to quit before Force quit is suggested


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
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
        self.bus.job_changed.connect(self.on_job)
        self.bus.sign_in_requested.connect(self.on_sign_in_requested)
        self.bus.updates_checked.connect(self.on_updates)
        self.bus.launch_phase.connect(self.on_phase)
        self.bus.task_finished.connect(self.on_task_finished)
        self.bus.update_needed.connect(self.on_update_needed)
        self.bus.storage_ready.connect(self.on_storage)
        self.bus.catalog_checked.connect(self.on_catalog)
        self.bus.game_closed.connect(self.on_game_closed)
        self.bus.running_scanned.connect(self.on_running_scanned)
        # Shift turns Close game into Force quit while it is held.
        QApplication.instance().installEventFilter(self)
        self.storage: dict = {}
        # The last list from the version service, so the window starts without waiting for it.
        catalog.apply((discovery.load(cache_dir(), self.settings.region) or {}).get('products'))
        self.catalog_state = 'ready' if catalog.checked() else 'checking'
        self._discovering: set[str] = set()     # regions being checked
        self._catalog_region = self.settings.region
        self._listed: list = []
        self.open_library()

        self.setWindowTitle('Waystone')
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
        self.uninstall_page = UninstallPage()
        self.uninstall_page.cancelled.connect(self.show_client_page)
        self.uninstall_page.confirmed.connect(self.confirm_uninstall)
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
                     self.options_page, self.uninstall_page]:
            self.stack.addWidget(page)
        right.addWidget(self.stack, 1)
        self.download_bar = DownloadBar()
        self.download_bar.pause_clicked.connect(lambda code: self.downloads.pause(code))
        self.download_bar.resume_clicked.connect(lambda code: self.downloads.enqueue(code, self.downloads.job(code).kind))
        self.download_bar.cancel_clicked.connect(lambda code: self.downloads.cancel(code))
        self.download_bar.open_client.connect(self.open_client)
        self.download_bar.limit_changed.connect(self.set_limit)
        self.download_bar.show_queue.connect(self.open_queue)
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

        self.populate()
        self.refresh()
        self.check_updates()
        self.discover()
        self.measure_storage()
        # Messages for every page, one at a time. The next shows once the current one is dismissed,
        # if it still applies then.
        self.startup_notices: list[tuple] = []
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
        update_timer = QTimer(self)
        update_timer.timeout.connect(self.check_updates)
        update_timer.timeout.connect(self.discover)
        update_timer.start(30 * 60 * 1000)


    def open_library(self):
        if not self.settings.root:
            return
        if self.downloads:
            self.downloads.shutdown()
        self.library = Library(self.settings.root)
        self.downloads = DownloadManager(self.settings, self.library)
        self.downloads.listeners.append(self.bus.job_changed.emit)

    # Layout

    def build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(280)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(SPACING, 18, SPACING, 12)
        side.setSpacing(0)
        brand = label('WAYSTONE', 'brand')
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
        self.account_button = QPushButton('')
        self.account_button.setObjectName('flat')
        self.account_button.clicked.connect(self.toggle_sign_in)
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

    def signed_in(self) -> bool:
        from ..auth.vault import Vault
        return Vault(data_dir() / 'session', None).has_session()

    def game_accounts(self) -> dict | None:
        """The WoW accounts of the signed-in login, as the last sign-in or launch listed them."""
        from ..auth.vault import Vault
        return Vault(data_dir() / 'session', None).accounts()

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

    def pick_game_account(self, owner: int, account: int | None) -> None:
        """Save the WoW account for this Battle.net login. None lets the game ask again."""
        if account is None:
            self.settings.game_accounts.pop(str(owner), None)
        else:
            self.settings.game_accounts[str(owner)] = account
        self.settings.save()
        self.refresh()

    def running_now(self) -> set[str]:
        """Clients whose game runs, including one Waystone did not start.

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
                self.bus.running_scanned.emit(library, found)
            threading.Thread(target=work, name='waystone-running', daemon=True).start()
        return self._running if self.library else set()

    @staticmethod
    def scan_running(library) -> set[str]:
        installed = library.installed()
        names = {path.name.lower() for path in map(library.executable, installed) if path}
        processes = system.running_executables(names)
        return {code for code in installed if library.running(code, processes)}

    def on_running_scanned(self, library, found) -> None:
        self._scanning = False
        self._running_at = time.monotonic()
        if library is self.library and found is not None and found != self._running:
            self._running = found
            self.refresh()

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
            return (f'{verb} {job.fraction:.0%}' if job.total_bytes else verb), 'active'
        if job and job.status == QUEUED:
            return 'Queued', 'active'
        if job and job.status == PAUSED:
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
        for code, row in self.client_list.rows.items():
            text, _ = self.status(code)
            state = self.library.state(code) if self.library else None
            row.status.setText(f"Ready · {state['version']}" if text == 'Ready' else text)
        signed = self.signed_in()
        signing_in = self.stack.currentWidget() is self.sign_in_page
        self.account.setText('Signing in…' if signing_in else
                             '<span style="color:#7fcf8f">●</span> Signed in' if signed else 'Not signed in')
        self.account_button.setText('Sign out' if signed else 'Sign in')
        self.show_game_account(signed and not signing_in)
        can_sign_in = bool(self.library and self.library.installed())
        # A second sign-in, or a sign-out, during a sign-in would race for the one ticket.
        # A launch uses the saved session, so it cannot be signed out of mid-launch either.
        launching_any = any(phase != 'Running' for phase in self.phases.values())
        self.account_button.setEnabled(can_sign_in and not signing_in and not self._signing_in
                                       and not launching_any)
        self.account_button.setToolTip('Install a client first. Signing in needs its version.' if not can_sign_in
                                       else 'Available once the game has started' if launching_any
                                       else 'Available after the sign-in' if signing_in or self._signing_in else '')
        self.refresh_downloads()
        code = self.selected()
        if code is None:
            self.client_page.show_empty()
            self.update_tab_order()
            return
        self.refresh_client(code)
        if self.stack.currentWidget() is self.uninstall_page:
            self.uninstall_page.set_reason(self.uninstall_reason(self.uninstall_page.code))
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
            primary = 'Running' if phase == 'Running' else phase.split(' for ')[0] + '…'
        elif not state:
            primary = 'Install'
        elif self.updates.get(code):
            primary = 'Update'
        else:
            # Play signs in first when there is no session, so the button says so.
            primary = 'Play' if signed else 'Sign in and play'
        progress, progress_text = None, ['']
        preparing = bool(downloading and job.status == RUNNING and not job.total_bytes)
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
                            and ((force or code not in self.closing) if running else not launching),
            primary_tip=self.primary_tip(code),
            installed=bool(state),
            busy=(launching and self.phases[code] != 'Running') or preparing, progress=progress,
            busy_launch=launching, queued=bool(job and job.status == QUEUED),
            progress_text=progress_text, cancellable=downloading and not removing, paused=bool(job and job.status == PAUSED),
            modes=[(m.id, m.label) for m in client.modes], mode=self.settings.modes.get(code),
            facts=self.facts(code), hint=hint, hideable=code not in self.kept_split()[1])
        reason = self.uninstall_reason(code)
        self.client_page.uninstall_action.setEnabled(bool(state) and not reason)
        self.client_page.uninstall_action.setToolTip(reason)
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
        if self.game_running(code):
            return ('Closes the game as its window\'s close button does. Hold Shift to force quit, '
                    'which ends it at once, as when it hangs.')
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
        self.settings_page.load(self.settings, *self.kept_split())
        if section:
            self.settings_page.show_section(section)
        self.settings_page.lock_folder(self.folder_lock())
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
        self.settings_page.set_root(folder)
        self.stack.setCurrentWidget(self.settings_page)

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
        if job and job.status == FAILED:
            self.clear_notice(code)
            self.downloads.enqueue(code, 'repair' if job.kind == 'verify' else job.kind)
        elif job and job.status in {QUEUED, RUNNING}:
            self.downloads.pause(code)
        elif job and job.status == PAUSED:
            self.downloads.enqueue(code, job.kind)
        elif not self.library.state(code):
            self.downloads.enqueue(code, 'install')
        elif self.updates.get(code):
            self.downloads.enqueue(code, 'update')
        else:
            self.play(code)
        self.refresh()

    def close_game(self, code: str, *, force: bool = False) -> None:
        executable, processes = self.library.executable(code), self.library.game_processes(code)
        if not force:
            # The grace time counts from the click, however long the request itself takes.
            asked = self.closing[code] = time.monotonic()
            QTimer.singleShot(CLOSE_GRACE * 1000, self, lambda: self.close_overdue(code, asked))
        self.refresh()

        def work():
            try:
                result = self.launcher().runtime.close(executable, processes, force=force)
            except Exception as error:  # shown in the banner
                result = str(error) or 'The game could not be closed.'
            self.bus.game_closed.emit(code, result, force)

        threading.Thread(target=work, name='waystone-close', daemon=True).start()

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

    def play(self, code: str):
        client = catalog.get(code)
        mode = self.settings.modes.get(code) or (client.modes[0].id if client.modes else None)
        self.clear_notice()
        self.launch_failed.discard(code)
        self.phases[code] = 'Checking for updates'
        self.refresh()

        def work():
            try:
                cdn = CDNClient(cache_dir(), self.library.state(code)['region'], code)
                if update_available(self.library, code, cdn):
                    self.bus.update_needed.emit(code)
                    return
                process = self.launcher().play(code, self.request_sign_in, mode=mode,
                                               status=lambda text: self.bus.launch_phase.emit(code, text))
                self.bus.launch_phase.emit(code, 'Running')
                self.settings.last_played[code] = int(time.time())
                process.wait()
                self.bus.task_finished.emit(code, '')
            except Exception as error:  # shown in the banner
                self.bus.task_finished.emit(code, str(error))

        threading.Thread(target=work, name='waystone-play', daemon=True).start()

    def request_sign_in(self, url: str) -> bytes:
        """Runs on a worker thread. Shows the sign-in page and waits for it."""
        self._sign_in_done.clear()
        self.bus.sign_in_requested.emit(url)
        self._sign_in_done.wait()
        if not self._sign_in_result or self._sign_in_result[0] is None:
            raise LaunchError('Sign-in was cancelled')
        return self._sign_in_result[0]

    def on_sign_in_requested(self, url: str):
        self.sign_in_page.start(url)
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

    def toggle_sign_in(self):
        launcher = self.launcher()
        if launcher.signed_in():
            launcher.vault.forget()
            self.refresh()
            return
        state = self.library.state(self.library.installed()[0])
        version = int(state['version'].rsplit('.', 1)[-1])
        self._signing_in = True
        self.refresh()

        def work():
            try:
                launcher.sign_in(version, self.request_sign_in)
                self.bus.task_finished.emit('', '')
            except Exception as error:
                self.bus.task_finished.emit('', str(error))

        threading.Thread(target=work, name='waystone-sign-in', daemon=True).start()

    def on_phase(self, code: str, text: str):
        self.phases[code] = text
        self.sync_game_downloads()
        self.refresh()

    def sync_game_downloads(self):
        if self.downloads:
            self.downloads.set_game_running(self.settings.pause_while_playing and
                                            'Running' in self.phases.values())

    def on_task_finished(self, code: str, message: str):
        self.phases.pop(code, None)
        self.closing.pop(code, None)
        self._running_at = float('-inf')        # the game is gone, so the list should not wait to say so
        self.sync_game_downloads()
        if not code:
            self._signing_in = False
        self.refresh()
        if not message or 'cancelled' in message.lower():
            return
        if code:
            # Seen in the list too, since another client may be selected by now.
            self.launch_failed.add(code)
        if code and not self.signed_in() and ('sign-in' in message.lower() or 'battle.net' in message.lower()):
            # The primary button now reads "Sign in and play", which is the way back.
            self.notify(message, 'error', client=code)
        elif code:
            self.notify(message, 'error', client=code)       # the main button below is the retry
        else:
            self.notify(message, client=code)
        self.refresh()

    def on_update_needed(self, code: str):
        self.phases.pop(code, None)
        self.updates[code] = True
        job = self.downloads.enqueue(code, 'update')
        where = 'It starts after the current download.' if job.status == QUEUED and self.downloads.busy() \
            else 'The update is running now.'
        self.notify(f'{catalog.get(code).label} needs an update before it can start. {where}', 'info', client=code)
        self.refresh()

    def measure_storage(self):
        if not self.library:
            return
        library = self.library

        def work():
            try:
                self.bus.storage_ready.emit(library.storage())
            except Exception:
                pass

        threading.Thread(target=work, name='waystone-storage', daemon=True).start()

    def on_storage(self, storage: dict):
        self.storage = storage
        self.refresh()

    def folder_lock(self) -> str:
        """Why the installation folder cannot change now, or nothing."""
        if self.downloads and (self.downloads.busy() or self.downloads.pending()):
            return 'Finish or cancel the downloads to choose another folder.'
        return ''

    def on_job(self, job: Job):
        self.settings_page.lock_folder(self.folder_lock())
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
            if job.result.get('backup'):
                message += f" Addons and settings saved in {Path(job.result['backup']).name}."
            if job.result.get('cleanup', {}).get('deferred'):
                message += ' Shared content cleanup will finish after other clients close or finish updating.'
            self.notify(message, 'info', client=job.client if job.client in self.client_list.rows else '')
        if job.status == FAILED:
            # The message shows on that client's own page, so it needs no name.
            # Dismiss also clears a failed download. Found damage stays until it is repaired.
            self.notify(job.error, 'error' if job.kind == 'verify' else 'failure', client=job.client)
        self.refresh()

    def check_updates(self):
        self._update_key = (self.settings.root, self.settings.region, self.settings.locale)
        if not self.library:
            return
        library = self.library

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
            self.bus.updates_checked.emit(found)

        threading.Thread(target=work, name='waystone-updates', daemon=True).start()

    def on_updates(self, found: dict):
        self.updates.update(found)
        self.refresh()

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
            self.bus.catalog_checked.emit(region, result)

        threading.Thread(target=work, name='waystone-catalog', daemon=True).start()

    def on_catalog(self, region: str, snapshot):
        self._discovering.discard(region)
        if region != self.settings.region:
            self.discover()         # the region changed while this one was checked
            return
        if snapshot:
            catalog.apply(snapshot['products'])
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
            catalog.apply((discovery.load(cache_dir(), self.settings.region) or {}).get('products'))
            self.catalog_state = 'ready' if catalog.checked() else 'checking'
            self.discover()
        self.show_catalog()
        # Every typing pause saves. Only a new folder, region or language changes what is current.
        key = (self.settings.root, self.settings.region, self.settings.locale)
        if key != self._update_key:
            self.check_updates()

    def closeEvent(self, event):
        # Every event in the application passes the filter, so it goes with the window.
        QApplication.instance().removeEventFilter(self)
        if self.downloads:
            self.downloads.shutdown()
        if self.downloads and self.downloads.busy():
            # Keep stored content and the queue for the next start.
            self.notify('Pausing downloads before closing…', 'info', client='')
            self.stack.setCurrentWidget(self.client_page)
            QApplication.processEvents()
            deadline = time.monotonic() + 30
            while self.downloads.busy() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.05)
        self.settings.save()
        event.accept()


def prepare(application: QApplication) -> None:
    """A fixed style, palette and stylesheet, so desktop themes such as Breeze do not mix in."""
    application.setStyle(QStyleFactory.create('Fusion'))
    application.setPalette(dark_palette())
    application.setStyleSheet(stylesheet(make_images(cache_dir() / 'ui')))


def close_on_signals(window: QMainWindow) -> None:
    """Ctrl+C or a terminate request closes the window as its close button would.

    Qt swallows the KeyboardInterrupt Python raises inside its callbacks, so without this
    the terminal cannot stop the launcher. A second Ctrl+C quits at once.
    """
    def close(number, frame):
        signal.signal(number, signal.SIG_DFL)
        QTimer.singleShot(0, window.close)      # outside whatever callback the signal interrupted
    signal.signal(signal.SIGINT, close)
    signal.signal(signal.SIGTERM, close)


def main():
    # Qt Wayland reports harmless text-input focus mismatches on every focus change.
    os.environ.setdefault('QT_LOGGING_RULES', 'qt.qpa.wayland.textinput=false')
    # Waystone sets its own style. A desktop override such as Breeze only prints a warning.
    os.environ.pop('QT_STYLE_OVERRIDE', None)
    application = QApplication(sys.argv)
    application.setApplicationName('Waystone')
    prepare(application)
    window = MainWindow()
    close_on_signals(window)
    window.show()
    sys.exit(application.exec())
