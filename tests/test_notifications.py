"""Desktop notifications reach the freedesktop notification service, on a private session bus."""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')
pytest.importorskip('jeepney')
pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='the freedesktop service is Linux only')

from jeepney import DBusAddress, HeaderFields, MessageType, message_bus, new_method_return, new_signal  # noqa: E402
from jeepney.io.blocking import open_dbus_connection  # noqa: E402

from waystone.ui.notifications import Notifier  # noqa: E402

# Only what a session bus needs. No service folders, so nothing on this machine is started.
CONFIG = '''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:dir={folder}</listen>
<auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/>
</policy></busconfig>'''


@pytest.fixture(scope='module')
def qt():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def session_bus(tmp_path):
    daemon = shutil.which('dbus-daemon') or pytest.skip('needs dbus-daemon')
    (tmp_path / 'bus.conf').write_text(CONFIG.format(folder=tmp_path))
    process = subprocess.Popen([daemon, '--config-file', str(tmp_path / 'bus.conf'), '--nofork', '--print-address'],
                               stdout=subprocess.PIPE, text=True)
    yield process.stdout.readline().strip()
    process.terminate()
    process.wait(5)


class NotificationService:
    """Answers as a desktop's notification service does, and sends a click only to the
    connection that showed the notification, as KDE's and dunst do."""

    def __init__(self):
        self.connection = open_dbus_connection(bus='SESSION')
        self.connection.send_and_get_reply(message_bus.RequestName('org.freedesktop.Notifications'))
        self.shown: queue.Queue = queue.Queue()
        self.clicks: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        number = 0
        while not self.stop.is_set():
            for id, sender in iter_queue(self.clicks):
                signal = new_signal(DBusAddress('/org/freedesktop/Notifications', interface='org.freedesktop.Notifications'),
                                    'ActionInvoked', 'us', (id, 'default'))
                signal.header.fields[HeaderFields.destination] = sender
                self.connection.send(signal)
            try:
                message = self.connection.receive(timeout=0.05)
            except TimeoutError:
                continue
            if message.header.message_type != MessageType.method_call:
                continue
            member = message.header.fields[HeaderFields.member]
            if member == 'GetCapabilities':
                self.connection.send(new_method_return(message, 'as', (['actions', 'body-markup'],)))
            elif member == 'Notify':
                number += 1
                self.connection.send(new_method_return(message, 'u', (number,)))
                self.shown.put((number, message.header.fields[HeaderFields.sender],
                                message.header.fields[HeaderFields.signature], message.body))

    def close(self):
        self.stop.set()
        self.thread.join(5)
        self.connection.close()


def iter_queue(items):
    while True:
        try:
            yield items.get_nowait()
        except queue.Empty:
            return


def wait_for(qt, check):
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline
        qt.processEvents()
        time.sleep(0.01)


def test_a_notification_reaches_the_service_and_its_click_names_the_client(qt, session_bus, monkeypatch):
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', session_bus)
    service = NotificationService()
    notifier = Notifier()
    try:
        clicked = []
        notifier.clicked.connect(clicked.append)
        notifier.show('Classic Era did not install', 'The disk is full & the <download> stopped.', 'wow_classic_era',
                      'transfer.error')
        id, sender, signature, body = service.shown.get(timeout=5)
        assert signature == 'susssasa{sv}i'
        app, replaces, icon, title, text, actions, hints, timeout = body
        assert (app, replaces, title, timeout) == ('Waystone', 0, 'Classic Era did not install', -1)
        # The service reads markup, so the error's own characters are escaped.
        assert text == 'The disk is full &amp; the &lt;download&gt; stopped.'
        assert icon.startswith('file://') and icon.endswith('icon.svg')
        assert actions[0] == 'default'
        assert hints == {'desktop-entry': ('s', 'waystone'), 'category': ('s', 'transfer.error')}
        notifier.show('World of Warcraft is installed', 'Ready to play.', 'wow')
        second, *_ = service.shown.get(timeout=5)
        service.clicks.put((id, sender))
        wait_for(qt, lambda: clicked)
        assert clicked == ['wow_classic_era']
        service.clicks.put((second, sender))
        wait_for(qt, lambda: len(clicked) == 2)
        assert clicked[1] == 'wow'
    finally:
        notifier.close()
        service.close()


@pytest.mark.parametrize('missing', ['bus', 'service'])
def test_without_a_bus_or_service_a_notification_never_blocks_and_the_next_one_still_arrives(
        qt, session_bus, monkeypatch, tmp_path, missing):
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', f'unix:path={tmp_path}/gone' if missing == 'bus' else session_bus)
    notifier = Notifier()
    started = time.monotonic()
    notifier.show('World of Warcraft is installed', 'Ready to play.', 'wow')
    assert time.monotonic() - started < 0.5
    time.sleep(0.2)                     # the notification thread finds no bus or no service meanwhile
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', session_bus)
    service = NotificationService()
    try:
        notifier.show('Update for Forever', 'Version 12.0.1.1 is available.', 'wowf')
        _, _, _, body = service.shown.get(timeout=5)
        assert body[3] == 'Update for Forever'
        assert service.shown.empty()
    finally:
        notifier.close()
        service.close()
