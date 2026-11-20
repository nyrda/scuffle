"""A kept browser session, sent to Battle.net's sign-in address without a browser.

Battle.net answers a browser it still knows with a redirect to the address that holds the ticket,
before it shows any page. The request and the session's cookies are all that takes, so the sign-in
window's browser engine stays unloaded. A page in answer is for the user, and is left to the window.

Only the status, the address the answer points to and the cookies it sets are read, never a page.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
import http.client
import urllib.error
import urllib.parse
import urllib.request

from .. import system
from .bgs import log

# Whose cookies are kept. The page also loads a captcha and fonts from elsewhere.
KEPT_DOMAINS = ('battle.net', 'blizzard.com', 'battlenet.com.cn', 'blizzard.cn')
# What the bridge can protect, with room to spare.
SESSION_LIMIT = 30000
# Battle.net sent a known session on with its first answer. A few more allow for a detour.
REQUESTS = 5
TIMEOUT = 20
REDIRECTS = (301, 302, 303, 307, 308)
# The session is a browser's, so the page is asked for under a browser's name.
AGENT = (f'Mozilla/5.0 ({"Windows NT 10.0; Win64; x64" if system.WINDOWS else "X11; Linux x86_64"}) '
         'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
HEADERS = {'User-Agent': AGENT, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
           'Accept-Language': 'en'}


@dataclass
class Cookie:
    name: str
    value: str
    domain: str                 # without a leading dot
    subdomains: bool            # also sent to the hosts below the domain, as a cookie set with a Domain is
    path: str
    secure: bool
    expires: datetime | None    # None for a cookie that lasts as long as the browser is open
    line: str                   # as the kept session holds it, in the form Qt writes a cookie

    @property
    def key(self) -> tuple:
        return self.name, self.domain, self.path

    def expired(self) -> bool:
        return self.expires is not None and self.expires <= datetime.now(timezone.utc)

    def goes_to(self, address: urllib.parse.SplitResult) -> bool:
        host = (address.hostname or '').lower()
        if host != self.domain and not (self.subdomains and host.endswith('.' + self.domain)):
            return False
        path = address.path or '/'
        below = path.startswith(self.path) and (self.path.endswith('/') or path[len(self.path):][:1] == '/')
        return (path == self.path or below) and (address.scheme == 'https' or not self.secure)


def kept(domain: str) -> bool:
    domain = domain.lstrip('.').lower()
    return any(domain == name or domain.endswith('.' + name) for name in KEPT_DOMAINS)


def split(line: str) -> tuple[str, str, dict[str, str]]:
    """A cookie's name, its value and its attributes by their names in lower case."""
    first, *rest = line.split(';')
    name, _, value = first.partition('=')
    attributes = {}
    for part in rest:
        key, _, text = part.partition('=')
        attributes[key.strip().lower()] = text.strip()
    return name.strip(), value.strip(), attributes


def moment(text: str | None) -> datetime | None:
    """The time an expires attribute names. One that cannot be read counts as none, as browsers take it."""
    try:
        time = parsedate_to_datetime(text or '')
        return time.astimezone(timezone.utc) if time.tzinfo else time.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def kept_cookie(line: str) -> Cookie | None:
    """A cookie of the kept session, or None for a line that holds none of Battle.net's."""
    name, value, attributes = split(line)
    domain = attributes.get('domain', '')
    if not name or not kept(domain):
        return None
    return Cookie(name, value, domain.lstrip('.').lower(), domain.startswith('.'), attributes.get('path') or '/',
                  'secure' in attributes, moment(attributes.get('expires')), line)


def set_cookie(header: str, address: urllib.parse.SplitResult) -> Cookie | None:
    """The cookie an answer from address sets, or None when it is not that host's to set or not Battle.net's."""
    name, value, attributes = split(header)
    host = (address.hostname or '').lower()
    domain = attributes.get('domain', '').lstrip('.').lower()
    if not name or not kept(domain or host) or (domain and host != domain and not host.endswith('.' + domain)):
        return None
    expires = moment(attributes.get('expires'))
    try:
        expires = datetime.now(timezone.utc) + timedelta(seconds=int(attributes['max-age']))
    except (KeyError, ValueError, OverflowError):
        pass        # no lifetime in seconds, so the date counts
    folder = address.path[:address.path.rfind('/')] or '/'
    cookie = Cookie(name, value, domain or host, bool(domain), attributes.get('path') or folder,
                    'secure' in attributes, expires, '')
    cookie.line = written(cookie, attributes)
    return cookie


def written(cookie: Cookie, attributes: dict[str, str]) -> str:
    """A cookie as Qt writes one, so the sign-in window's browser reads the session too."""
    parts = [f'{cookie.name}={cookie.value}']
    if cookie.secure:
        parts.append('secure')
    if 'httponly' in attributes:
        parts.append('HttpOnly')
    if attributes.get('samesite'):
        parts.append(f'SameSite={attributes["samesite"]}')
    if cookie.expires is not None:
        parts.append('expires=' + format_datetime(cookie.expires, usegmt=True))
    return '; '.join([*parts, f'domain={"." if cookie.subdomains else ""}{cookie.domain}', f'path={cookie.path}'])


def unpack(session: bytes) -> dict[tuple, Cookie]:
    cookies = (kept_cookie(line) for line in session.decode('latin-1').split('\n'))
    return {cookie.key: cookie for cookie in cookies if cookie is not None and not cookie.expired()}


def pack(jar: dict[tuple, Cookie]) -> bytes | None:
    """The session as bytes, one cookie to a line. None when it is more than can be protected."""
    data = '\n'.join(sorted(cookie.line for cookie in jar.values() if not cookie.expired())).encode('latin-1')
    return data if len(data) <= SESSION_LIMIT else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Each answer is looked at here, so no redirect is followed unseen."""

    def redirect_request(self, *_):
        return None


def ask(opener, url: str, jar: dict[tuple, Cookie]) -> tuple[int, str | None]:
    """Ask for url with the session's cookies. Returns the status and where the answer points, and keeps its cookies."""
    address = urllib.parse.urlsplit(url)
    cookies = '; '.join(f'{cookie.name}={cookie.value}' for cookie in jar.values()
                        if cookie.goes_to(address) and not cookie.expired())
    request = urllib.request.Request(url, headers=HEADERS | ({'Cookie': cookies} if cookies else {}))
    try:
        answer = opener.open(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as error:
        answer = error          # also a redirect, which is not followed by itself
    with answer:
        for header in answer.headers.get_all('Set-Cookie') or []:
            cookie = set_cookie(header, address)
            if cookie is None:
                continue
            jar.pop(cookie.key, None)
            if not cookie.expired():        # a cookie is removed by setting it with a time that has passed
                jar[cookie.key] = cookie
        return answer.status, answer.headers.get('Location')


def is_the_end(url: str) -> bool:
    """Whether url is the address a sign-in ends at, http://localhost:0 with a ticket, which nothing can open."""
    address = urllib.parse.urlsplit(url)
    return (address.scheme == 'http' and address.netloc == 'localhost:0' and address.path in ('', '/')
            and 'ST' in urllib.parse.parse_qs(address.query))


def stays_with_battlenet(url: str, target: str) -> bool:
    """Whether a redirect from url to target stays on Battle.net's sites and never leaves HTTPS.

    A path with a step up or in place is not followed. A browser resolves those before it picks the
    cookies to send, and this asks for the path as it is written.
    """
    before, after = urllib.parse.urlsplit(url), urllib.parse.urlsplit(target)
    if any(urllib.parse.unquote(step) in ('.', '..') for step in after.path.split('/')):
        return False
    return kept(after.hostname or '') and (after.scheme == 'https' or before.scheme == after.scheme == 'http')


def follow(url: str, session: bytes) -> tuple[str, bytes] | None:
    """The address Battle.net sends a browser with this session on to, and the session to keep after that.

    None when Battle.net shows a page instead, which is for the user, or does not answer.
    """
    jar = unpack(session)
    if not jar or not kept(urllib.parse.urlsplit(url).hostname or ''):
        return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        for _ in range(REQUESTS):
            status, location = ask(opener, url, jar)
            target = urllib.parse.urljoin(url, location) if status in REDIRECTS and location else None
            if target is None or not (is_the_end(target) or stays_with_battlenet(url, target)):
                log('kept_browser_session', 'answered_with', 'a page' if target is None else 'another site')
                return None
            if is_the_end(target):
                log('kept_browser_session', 'answered_with', 'a ticket')
                packed = pack(jar)
                # One too large to protect keeps what it was. An empty one stays empty: Battle.net took its cookies back.
                return target, session if packed is None else packed
            url = target
        log('kept_browser_session', 'answered_with', 'too many redirects')
    except (OSError, http.client.HTTPException, ValueError) as error:
        log('kept_browser_session', 'not_answered', type(error).__name__)
    return None
