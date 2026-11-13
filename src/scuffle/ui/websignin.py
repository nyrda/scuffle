"""Battle.net's sign-in page in a view of Scuffle's own.

The view's browser session lives in memory only. Its cookies are handed back with the ticket,
for the vault to keep protected, and are put back for the next sign-in. Battle.net's page then
signs in by itself, as a browser that is still signed in does, and nobody has to type anything.

Scuffle never reads the page or what is typed into it. It reads the address the page is sent to
after the sign-in, which holds the ticket, and the cookies Battle.net sets.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Iterable

from PySide6.QtCore import QDateTime, QLocale, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from ..launch import looks_like_redirect, parse_ticket
from .style import BACKGROUND
from .widgets import label

if TYPE_CHECKING:
    # Only where the browser engine is there. The AppImage's Qt has neither, and imports this module too.
    from PySide6.QtNetwork import QNetworkCookie

# Whose cookies are kept. The page also loads a captcha and fonts from elsewhere.
KEPT_DOMAINS = ('battle.net', 'blizzard.com', 'battlenet.com.cn', 'blizzard.cn')
# What the bridge can protect, with room to spare.
SESSION_LIMIT = 30000
# After the page loaded and before it is shown: a page that knows the account redirects by then.
SETTLE = 1500
# A page that neither loads nor redirects is shown after this long, so the user sees why.
PATIENCE = 20000
# Cookies the redirect's own answer sets are reported a moment after the redirect.
COOKIE_WAIT = 400
MINIMUM_HEIGHT = 240

_engine = None


def available() -> bool:
    """Whether this build has Qt's browser engine. A package without it signs in through the user's browser."""
    try:
        engine()
    except ImportError:
        return False
    return True


def engine():
    """Qt's browser engine, loaded on first use, since most starts never sign in."""
    global _engine
    if _engine is None:
        if QGuiApplication.platformName() == 'offscreen':
            # No display means no GPU to draw with, and the engine stops the process over that.
            flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
            os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = f'{flags} --disable-gpu'.strip()
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        class Page(QWebEnginePage):
            redirected = Signal(str)

            def acceptNavigationRequest(self, url, kind, main_frame):
                # The sign-in ends at http://localhost:0, which nothing can open. The address is all that is read.
                if (main_frame and url.scheme() == 'http' and url.host() == 'localhost' and url.port() == 0
                        and looks_like_redirect(url.toString())):
                    self.redirected.emit(url.toString())
                    return False
                return True

        class Engine:
            pass
        Engine.Page, Engine.Profile, Engine.View = Page, QWebEngineProfile, QWebEngineView
        _engine = Engine
    return _engine


def kept(cookie: QNetworkCookie) -> bool:
    domain = cookie.domain().lstrip('.').lower()
    return any(domain == name or domain.endswith('.' + name) for name in KEPT_DOMAINS)


def expired(cookie: QNetworkCookie) -> bool:
    return not cookie.isSessionCookie() and cookie.expirationDate() < QDateTime.currentDateTimeUtc()


def pack(cookies: Iterable[QNetworkCookie]) -> bytes:
    """A browser session as bytes: Battle.net's cookies, one to a line. Empty when there is none to keep."""
    lines = sorted(bytes(cookie.toRawForm()) for cookie in cookies if kept(cookie) and not expired(cookie))
    data = b'\n'.join(line for line in lines if b'\n' not in line)
    return data if len(data) <= SESSION_LIMIT else b''


def unpack(data: bytes) -> list[QNetworkCookie]:
    from PySide6.QtNetwork import QNetworkCookie
    cookies = [cookie for line in data.split(b'\n') for cookie in QNetworkCookie.parseCookies(line)]
    return [cookie for cookie in cookies if kept(cookie) and not expired(cookie)]


class WebSignIn(QWidget):
    """Shows Battle.net's sign-in page and hands back the ticket and the browser session."""

    finished = Signal(bytes, bytes)      # the ticket, and the session's cookies
    asks = Signal()                      # the page has something for the user to do

    def __init__(self):
        super().__init__()
        self.view = self.page = self.profile = None
        self.asked = False
        self._ticket = None
        self._jar: dict[tuple, QNetworkCookie] = {}
        self._waiting: set[tuple] = set()
        self._url = QUrl()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        # Which site the view shows, as a browser's address bar says it.
        self.place = label('', 'small')
        layout.addWidget(self.place)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._settle = self._timer(SETTLE, self._ask)
        self._patience = self._timer(PATIENCE, self._ask)
        self._cookies_set = self._timer(1000, self._load)
        self._cookies_in = self._timer(COOKIE_WAIT, self._finish)

    def _timer(self, interval: int, handler) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(handler)
        return timer

    def start(self, url: str, web_session: bytes | None = None) -> None:
        """Open the sign-in page, signed in already when web_session is a session Battle.net still knows."""
        self.stop()
        parts = engine()
        self.asked, self._ticket, self._url = False, None, QUrl(url)
        self.profile = parts.Profile()          # without a name it keeps nothing on disk
        self.profile.setHttpAcceptLanguage(','.join(QLocale.system().uiLanguages()))
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self._added)
        store.cookieRemoved.connect(self._removed)
        self.page = parts.Page(self.profile)
        self.page.setBackgroundColor(QColor(BACKGROUND))     # not a white flash before the page is there
        self.page.redirected.connect(self._redirected)
        self.page.loadStarted.connect(self._settle.stop)
        self.page.loadFinished.connect(self._loaded)
        self.page.urlChanged.connect(self._show_place)
        self.page.newWindowRequested.connect(self._open_outside)
        self.view = parts.View()
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        # As tall as the window has room for. The engine's own wish of 600 pixels would make the page scroll.
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.view.setMinimumHeight(MINIMUM_HEIGHT)
        self.layout().addWidget(self.view, 1)
        self.setFocusProxy(self.view)
        self._show_place(self._url)
        cookies = unpack(web_session) if web_session else []
        self._waiting = {self._key(cookie) for cookie in cookies}
        for cookie in cookies:
            store.setCookie(cookie, QUrl(f'https://{cookie.domain().lstrip(".")}{cookie.path() or "/"}'))
        # The page is asked for once the cookies are in, or after a second if one never is.
        if cookies:
            self._cookies_set.start()
        else:
            self._load()

    def stop(self) -> None:
        """End the browser session. Nothing of it stays in memory or on disk."""
        for timer in (self._settle, self._patience, self._cookies_set, self._cookies_in):
            timer.stop()
        view, page, profile = self.view, self.page, self.profile
        self.view = self.page = self.profile = None
        self._jar.clear()
        self._waiting.clear()
        if view is None:
            return
        self.setFocusProxy(None)
        self.layout().removeWidget(view)
        view.hide()
        # In this order: a profile must outlive its page, and a page its view.
        for part in (view, page, profile):
            part.deleteLater()

    @staticmethod
    def _key(cookie: QNetworkCookie) -> tuple:
        return cookie.name().data(), cookie.domain().lstrip('.'), cookie.path()

    def _added(self, cookie: QNetworkCookie) -> None:
        from PySide6.QtNetwork import QNetworkCookie
        self._jar[self._key(cookie)] = QNetworkCookie(cookie)
        if self._waiting:
            self._waiting.discard(self._key(cookie))
            if not self._waiting and self._cookies_set.isActive():
                self._cookies_set.stop()
                self._load()

    def _removed(self, cookie: QNetworkCookie) -> None:
        self._jar.pop(self._key(cookie), None)

    def _load(self) -> None:
        if self.page is not None:
            self._patience.start()
            self.page.load(self._url)

    def _loaded(self, _ok: bool) -> None:
        # A page that knows the account goes on to the redirect. One that stays wants the user.
        if self.page is not None and self._ticket is None:
            self._settle.start()

    def _ask(self) -> None:
        self._patience.stop()
        if self.page is not None and self._ticket is None and not self.asked:
            self.asked = True
            self.asks.emit()

    def _redirected(self, address: str) -> None:
        if self._ticket is not None:
            return
        try:
            self._ticket = parse_ticket(address)
        except ValueError:
            self._ask()
            return
        self._settle.stop()
        self._patience.stop()
        self._cookies_in.start()

    def _finish(self) -> None:
        ticket, session = self._ticket, pack(self._jar.values())
        self.stop()
        self.finished.emit(ticket, session)

    def _show_place(self, url: QUrl) -> None:
        self.place.setText(f'{url.scheme()}://{url.host()}' if url.host() else '')

    def _open_outside(self, request) -> None:
        # A link that asks for a window of its own, such as the privacy policy, goes to the user's browser.
        if request.requestedUrl().scheme() == 'https':
            QDesktopServices.openUrl(request.requestedUrl())
