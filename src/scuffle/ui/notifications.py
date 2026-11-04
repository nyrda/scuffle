"""Desktop notifications through the system's own notification service.

On Linux they go to the freedesktop notification service (org.freedesktop.Notifications) on
the session bus, so the desktop's do-not-disturb applies and they show without a tray. On
Windows, and on Linux without that service, the tray icon shows them as the desktop does.
"""
from __future__ import annotations

import html
import queue
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QSystemTrayIcon

SERVICE = 'org.freedesktop.Notifications'
PATH = '/org/freedesktop/Notifications'
TIMEOUT = 5         # seconds the service gets to answer, on the notification thread only
ICON = QUrl.fromLocalFile(str(Path(__file__).with_name('icon.svg'))).toString()


class Notifier(QObject):
    """Shows a notification about one client. Clicking it emits clicked with that client, and
    one of its buttons emits acted with the client and the button's key.

    Buttons need the freedesktop service. The tray icon's message has none, so its click only
    opens the client, whose page offers the same choices.

    The D-Bus work runs on a thread of its own, so a missing, slow or hung service never holds
    up the window. Any failure falls back to the tray icon, or to nothing, and never raises.
    """

    clicked = Signal(str)
    acted = Signal(str, str)             # client, the key of the button
    _unsent = Signal(str, str, str)      # title, body, client the service did not take
    _invoked = Signal(str)               # the client of a clicked notification, from the thread
    _acted = Signal(str, str)            # the client and button of a notification, from the thread

    def __init__(self, tray: QSystemTrayIcon | None = None):
        super().__init__()
        self.tray = tray
        # Notifications to send and the service's signals. A SimpleQueue cannot be shut down, so
        # closing a failed connection never stops later notifications.
        self._work: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._tray_client = ''                      # the tray's message has no id, so the last one counts
        self._unsent.connect(self.show_in_tray)
        self._invoked.connect(self.clicked.emit)
        self._acted.connect(self.acted.emit)
        if tray is not None:
            tray.messageClicked.connect(lambda: self._tray_client and self.clicked.emit(self._tray_client))

    def show(self, title: str, body: str, client: str, category: str = '', actions: tuple = ()) -> None:
        """actions are (key, label) pairs, shown as buttons where the service has them."""
        if not sys.platform.startswith('linux'):
            self.show_in_tray(title, body, client)
            return
        self._work.put((title, body, client, category, tuple(actions)))
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='scuffle-notifications', daemon=True)
            self._thread.start()

    def show_in_tray(self, title: str, body: str, client: str) -> None:
        tray = self.tray
        if tray is None or not tray.isVisible() or not QSystemTrayIcon.supportsMessages():
            return
        self._tray_client = client
        tray.showMessage(title, body, tray.icon())

    def close(self) -> None:
        """Close the bus connection on quit, waiting a moment for a notification being sent."""
        if self._thread is not None:
            self._work.put(None)
            self._thread.join(1)

    # The notification thread

    def _run(self) -> None:
        bus = None
        ids: dict[int, tuple] = {}      # the service's id of each notification -> its client and button keys
        while True:
            item = self._work.get()
            if item is None:
                _close(bus)
                return
            if isinstance(item, tuple):
                title, body, client, category, actions = item
                try:
                    bus = bus or Bus(self._work)
                    ids[bus.notify(title, body, category, actions)] = (client, {key for key, _ in actions})
                except Exception:   # no session bus, no service, a timeout or no jeepney
                    _close(bus)
                    bus = None          # connected again for the next one
                    self._unsent.emit(title, body, client)
                continue
            # A signal from the service, which only arrives once jeepney is there.
            from jeepney import HeaderFields
            member, arguments = item.header.fields.get(HeaderFields.member), item.body
            if member == 'ActionInvoked' and len(arguments) == 2 and arguments[0] in ids:
                client, keys = ids[arguments[0]]
                if arguments[1] == 'default':
                    self._invoked.emit(client)
                elif arguments[1] in keys:     # only a button this notification has
                    self._acted.emit(client, arguments[1])
            elif member == 'NotificationClosed' and arguments:
                ids.pop(arguments[0], None)


def _close(bus) -> None:
    try:
        if bus is not None:
            bus.close()
    except Exception:       # a connection that broke is closed already
        pass


class Bus:
    """A session bus connection that stays open, since some services, such as KDE's and
    dunst, send a click only to the connection that showed the notification."""

    def __init__(self, signals: queue.SimpleQueue):
        from jeepney import MatchRule, message_bus
        from jeepney.io.threading import DBusRouter, open_dbus_connection
        self.connection = open_dbus_connection(bus='SESSION')
        try:
            self.router = DBusRouter(self.connection)
            rule = MatchRule(type='signal', interface=SERVICE, path=PATH)
            self.router.filter(rule, queue=signals)
            # Other services broadcast their signals, which the bus forwards only on request.
            self.call(message_bus.AddMatch(rule))
            self.markup = 'body-markup' in self.call(self.method('GetCapabilities'))[0]
        except BaseException:
            self.close()
            raise

    def method(self, name: str, signature: str | None = None, body: tuple = ()):
        from jeepney import DBusAddress, new_method_call
        return new_method_call(DBusAddress(PATH, bus_name=SERVICE, interface=SERVICE), name, signature, body)

    def call(self, message) -> tuple:
        from jeepney.wrappers import unwrap_msg
        return unwrap_msg(self.router.send_and_get_reply(message, timeout=TIMEOUT))

    def notify(self, title: str, body: str, category: str, actions: tuple = ()) -> int:
        hints = {'desktop-entry': ('s', 'scuffle')}
        if category:
            hints['category'] = ('s', category)
        # A service with markup would read an error's < or & as a tag.
        text = html.escape(body, quote=False) if self.markup else body
        # The default action is the click on the body. Each button follows as its key and label.
        buttons = ['default', 'Open Scuffle', *(part for action in actions for part in action)]
        return self.call(self.method('Notify', 'susssasa{sv}i', (
            'Scuffle', 0, ICON, title, text, buttons, hints, -1)))[0]

    def close(self) -> None:
        router = getattr(self, 'router', None)
        if router is not None:
            router.close()
        self.connection.close()
