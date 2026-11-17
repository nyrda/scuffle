"""Sign in on a stand-in for Battle.net's page with the AppImage's own Python, Qt and browser engine.

Usage: <the AppImage's python3.14> -I -B check-sign-in.py

test-ubuntu.sh runs it in an extracted copy, without network. The stand-in sets a cookie and
sends the browser on to the address that holds the ticket, as Battle.net's page does for a
browser it knows. Every name the engine looks up is this machine, so the stand-in answers under
Battle.net's name and its cookie is one Scuffle keeps.
"""
import http.server
import os
import sys
import threading

TICKET = b'EU-0123abcd-314159265'
REDIRECT = f'http://localhost:0/?ST={TICKET.decode()}&flowTrackingId='


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header('Set-Cookie', 'login=known; Path=/; Max-Age=86400; HttpOnly')
        self.send_header('Location', REDIRECT)
        self.end_headers()

    def log_message(self, *_):
        pass


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--host-resolver-rules="MAP * 127.0.0.1"'

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
app = QApplication([])
from scuffle.ui import websignin  # noqa: E402

if not websignin.available():
    sys.exit('The browser engine does not load')
result = {}


def finished(ticket, session):
    result.update(ticket=ticket, session=session)
    app.quit()


view = websignin.WebSignIn()
view.finished.connect(finished)
view.asks.connect(app.quit)
QTimer.singleShot(60000, app.quit)
view.start(f'http://eu.account.battle.net:{server.server_address[1]}/login/?app=wow')
app.exec()
sandbox = 'off' if '--no-sandbox' in os.environ['QTWEBENGINE_CHROMIUM_FLAGS'].split() else 'on'
if result.get('ticket') != TICKET:
    sys.exit(f'The sign-in handed back no ticket, sandbox {sandbox}')
if b'login=known' not in result['session']:
    sys.exit(f'The sign-in kept no browser session, sandbox {sandbox}')
print(f'signed in with the sandbox {sandbox}')
