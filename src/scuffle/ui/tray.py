"""The tray icon: show the window, the downloads, the update check, a few clients to play, and Quit."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

PLAY_LIMIT = 5      # clients the menu offers to play, the most recently played first


class Tray(QObject):
    """The icon and its menu. The window fills the menu in from its own state."""

    show_window = Signal()
    pause_all = Signal()
    resume_all = Signal()
    play = Signal(str)
    check_now = Signal()
    self_update = Signal()
    quit = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(QIcon(str(Path(__file__).with_name('icon-small.svg'))), self)  # drawn for tray sizes
        self.icon.setToolTip('Scuffle')
        self.icon.activated.connect(self.on_activated)
        # A menu has no parent widget here, so it lives as long as the tray does.
        self.menu = QMenu()
        self.show_action = self.menu.addAction('Show Scuffle')
        self.show_action.triggered.connect(self.show_window.emit)
        self.self_update_action = self.menu.addAction('')
        self.self_update_action.triggered.connect(self.self_update.emit)
        self.self_update_action.setVisible(False)
        self.menu.addSeparator()
        self.downloads = self.menu.addAction('No downloads')
        self.downloads.setEnabled(False)
        self.pause_action = self.menu.addAction('Pause all')
        self.pause_action.triggered.connect(self.pause_all.emit)
        self.resume_action = self.menu.addAction('Resume all')
        self.resume_action.triggered.connect(self.resume_all.emit)
        self.check_separator = self.menu.addSeparator()
        self.next_check = self.menu.addAction('')
        self.next_check.setEnabled(False)
        self.check_action = self.menu.addAction('Check now')
        self.check_action.triggered.connect(self.check_now.emit)
        self.clients_separator = self.menu.addSeparator()
        self.client_actions: dict[str, object] = {}
        self.quit_separator = self.menu.addSeparator()
        self.quit_action = self.menu.addAction('Quit Scuffle')
        self.quit_action.triggered.connect(self.quit.emit)
        self.icon.setContextMenu(self.menu)
        self.icon.show()

    def available(self) -> bool:
        """Whether the icon is there to click, so a hidden window can always come back."""
        return self.icon.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()

    def on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window.emit()

    def show_state(self, downloads: str, can_pause: bool, can_resume: bool,
                   clients: list[tuple[str, str, bool]], next_check: str = '', can_check: bool = False,
                   self_update: str = '') -> None:
        """The download line, Pause all and Resume all, when the next update check runs, and each
        client's action text and whether it can start now. Without next_check there is nothing to
        check. Actions are changed in place, so an open menu is not rebuilt under the pointer."""
        self.self_update_action.setText(self_update)
        self.self_update_action.setVisible(bool(self_update))
        self.downloads.setText(downloads)
        for action in (self.check_separator, self.next_check, self.check_action):
            action.setVisible(bool(next_check))
        self.next_check.setText(next_check)
        self.check_action.setEnabled(can_check)
        self.icon.setToolTip(f'Scuffle\n{downloads}')
        self.pause_action.setVisible(can_pause or can_resume)
        self.resume_action.setVisible(can_pause or can_resume)
        self.pause_action.setEnabled(can_pause)
        self.resume_action.setEnabled(can_resume)
        codes = [code for code, _, _ in clients]
        if codes != list(self.client_actions):
            for action in self.client_actions.values():
                self.menu.removeAction(action)
                action.deleteLater()
            self.client_actions = {}
            for code in codes:
                action = QAction(self.menu)
                self.menu.insertAction(self.quit_separator, action)
                action.triggered.connect(lambda _=False, code=code: self.play.emit(code))
                self.client_actions[code] = action
        self.clients_separator.setVisible(bool(clients))
        for code, text, enabled in clients:
            self.client_actions[code].setText(text)
            self.client_actions[code].setEnabled(enabled)

    def hide(self) -> None:
        self.icon.hide()
