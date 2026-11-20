"""Signing in again with the kept browser session alone, without a browser."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from scuffle.auth import websession

KNOWN = b'login=known; HttpOnly; domain=.battle.net; path=/'


def test_a_session_battlenet_knows_is_sent_on_to_the_address_with_the_ticket(sign_in_site):
    address, session = websession.follow(sign_in_site.address, KNOWN)
    assert address == sign_in_site.REDIRECT
    # One request did it, with the session's cookie, and the address it ends at was never asked for.
    assert sign_in_site.asked == [('eu.account.battle.net', '/login/', 'login=known')]
    # What the answer set belongs to the session, and what was kept stays as it was.
    lines = session.split(b'\n')
    assert KNOWN in lines and len(lines) == 2
    [seen] = [line for line in lines if line.startswith(b'seen=again; HttpOnly; expires=')]
    assert seen.endswith(b'GMT; domain=eu.account.battle.net; path=/')


def test_a_session_battlenet_no_longer_knows_is_left_to_the_sign_in_window(sign_in_site):
    assert websession.follow(sign_in_site.address, b'login=ended; domain=.battle.net; path=/') is None
    assert len(sign_in_site.asked) == 1         # the page that came back is the user's, and was not sent anything


def test_a_cookie_goes_only_where_a_browser_would_send_it(sign_in_site):
    passed = format_datetime(datetime.now(timezone.utc) - timedelta(days=1), usegmt=True)
    later = format_datetime(datetime.now(timezone.utc) + timedelta(days=1), usegmt=True)
    session = '\n'.join([
        'login=known; domain=.battle.net; path=/',
        'sub=1; domain=.account.battle.net; path=/login',
        f'lasting=1; expires={later}; domain=eu.account.battle.net; path=/login/',
        'otherhost=1; domain=account.battle.net; path=/',          # set by that host for itself alone
        'otherpath=1; domain=.battle.net; path=/log',
        'deeper=1; domain=.battle.net; path=/login/creation',
        'encrypted=1; secure; domain=.battle.net; path=/',         # only over HTTPS, and the stand-in has none
        f'old=1; expires={passed}; domain=.battle.net; path=/',
        'theirs=1; domain=.example.com; path=/',
    ]).encode()
    _, kept = websession.follow(sign_in_site.address, session)
    [(_, _, cookies)] = sign_in_site.asked
    assert sorted(cookies.split('; ')) == ['lasting=1', 'login=known', 'sub=1']
    # A cookie that ran out and one of another site are not kept either.
    assert b'old=1' not in kept and b'theirs=1' not in kept and b'encrypted=1; secure' in kept


def test_a_detour_over_battlenets_own_sites_is_followed_with_each_sites_cookies(sign_in_site):
    onward = sign_in_site.at('eu.battle.net', '/login/again')
    sign_in_site.answers['/login/'] = 302, [('Location', onward), ('Set-Cookie', 'step=1; Domain=battle.net; Path=/')]
    sign_in_site.answers['/login/again'] = 302, [('Location', sign_in_site.REDIRECT)]
    session = KNOWN + b'\nhome=1; domain=eu.account.battle.net; path=/'
    address, kept = websession.follow(sign_in_site.address, session)
    assert address == sign_in_site.REDIRECT
    assert sign_in_site.asked == [('eu.account.battle.net', '/login/', 'login=known; home=1'),
                                  ('eu.battle.net', '/login/again', 'login=known; step=1')]
    assert b'step=1; domain=.battle.net; path=/' in kept.split(b'\n')


def test_a_redirect_to_another_site_is_not_followed(sign_in_site):
    sign_in_site.answers['/login/'] = 302, [('Location', sign_in_site.at('battle.net.example.com', '/login/'))]
    assert websession.follow(sign_in_site.address, KNOWN) is None
    assert [host for host, _, _ in sign_in_site.asked] == ['eu.account.battle.net']


def test_only_the_address_a_sign_in_ends_at_is_taken(sign_in_site):
    # Another page that only names that address, as a page trying to pass a ticket of its own might.
    sign_in_site.answers['/login/'] = 302, [('Location', '/elsewhere?next=http://localhost:0/&ST=EU-0fake000-1')]
    sign_in_site.answers['/elsewhere'] = 200, []
    assert websession.follow(sign_in_site.address, KNOWN) is None
    assert [path for _, path, _ in sign_in_site.asked] == ['/login/', '/elsewhere']


def test_a_redirect_whose_path_a_browser_would_resolve_first_is_not_followed(sign_in_site):
    for step in ('..', '%2E%2e', '.'):
        sign_in_site.asked.clear()
        sign_in_site.answers['/login/'] = 302, [('Location', sign_in_site.at('eu.battle.net', f'/login/{step}/other'))]
        assert websession.follow(sign_in_site.address, KNOWN) is None
        assert [path for _, path, _ in sign_in_site.asked] == ['/login/']


@pytest.mark.parametrize('end', ['https://localhost:0/?ST=EU-1-2', 'http://localhost:80/?ST=EU-1-2',
                                 'http://localhost/?ST=EU-1-2', 'http://localhost.battle.net:0/?ST=EU-1-2',
                                 'http://localhost:0/?next=ST', 'http://localhost:0/other?ST=EU-1-2',
                                 'http://user@localhost:0/?ST=EU-1-2', 'http://localhost:x/?ST=EU-1-2'])
def test_an_address_that_only_resembles_the_end_is_not_one(end):
    assert not websession.is_the_end(end)
    assert websession.is_the_end('http://localhost:0/?ST=EU-0123abcd-412345678&flowTrackingId=')


def test_redirects_without_end_are_given_up(sign_in_site):
    sign_in_site.answers['/login/'] = 302, [('Location', '/login/')]
    assert websession.follow(sign_in_site.address, KNOWN) is None
    assert len(sign_in_site.asked) == websession.REQUESTS


def test_battlenet_takes_a_cookie_back_and_sets_none_for_another_site(sign_in_site):
    sign_in_site.answers['/login/'] = 302, [
        ('Location', sign_in_site.REDIRECT),
        ('Set-Cookie', 'extra=; Domain=battle.net; Path=/login; Max-Age=0'),
        ('Set-Cookie', 'gone=; Domain=battle.net; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT'),
        ('Set-Cookie', 'foreign=1; Domain=example.com; Path=/'),
        ('Set-Cookie', 'parent=1; Domain=net; Path=/'),
        ('Set-Cookie', 'login=renewed; Domain=battle.net; Path=/; Secure; SameSite=None'),
        ('Set-Cookie', 'here=1'),
        # Written with spaces, which a browser takes off, and with a time no clock can hold.
        ('Set-Cookie', 'spaced = 1 ; Path = /login/deep ; Domain = battle.net ; '
                       'Expires = Fri, 31 Dec 9999 23:59:59 -0100'),
    ]
    session = KNOWN + b'\nextra=1; domain=.battle.net; path=/login\ngone=1; domain=.battle.net; path=/'
    _, kept = websession.follow(sign_in_site.address, session)
    assert kept.split(b'\n') == [b'here=1; domain=eu.account.battle.net; path=/login',
                                 b'login=renewed; secure; SameSite=None; domain=.battle.net; path=/',
                                 b'spaced=1; domain=.battle.net; path=/login/deep']


def test_a_session_battlenet_took_every_cookie_of_is_kept_empty(sign_in_site):
    sign_in_site.answers['/login/'] = 302, [('Location', sign_in_site.REDIRECT),
                                            ('Set-Cookie', 'login=; Domain=battle.net; Path=/; Max-Age=0')]
    assert websession.follow(sign_in_site.address, KNOWN) == (sign_in_site.REDIRECT, b'')


def test_a_session_too_large_to_protect_keeps_what_it_was(sign_in_site):
    sign_in_site.answers['/login/'] = 302, [('Location', sign_in_site.REDIRECT),
                                            ('Set-Cookie', f'big={"x" * websession.SESSION_LIMIT}; Path=/')]
    assert websession.follow(sign_in_site.address, KNOWN) == (sign_in_site.REDIRECT, KNOWN)


def test_no_answer_and_nothing_to_send_both_leave_it_to_the_window(sign_in_site):
    passed = format_datetime(datetime.now(timezone.utc) - timedelta(days=1), usegmt=True)
    assert websession.follow(sign_in_site.address, f'login=known; expires={passed}; domain=.battle.net; path=/'.encode()) is None
    assert websession.follow(sign_in_site.address, b'') is None
    assert websession.follow(sign_in_site.at('example.com', '/login/'), KNOWN) is None
    assert sign_in_site.asked == []
    sign_in_site.server.shutdown()
    sign_in_site.server.server_close()
    assert websession.follow(sign_in_site.address, KNOWN) is None


def test_the_sign_in_window_and_the_plain_request_read_each_others_session(sign_in_site):
    QtNetwork = pytest.importorskip('PySide6.QtNetwork')
    from scuffle.ui import websignin
    # As the sign-in window's browser hands its cookies back.
    cookie = QtNetwork.QNetworkCookie(b'login', b'known')
    cookie.setDomain('.battle.net')
    cookie.setPath('/')
    cookie.setHttpOnly(True)
    cookie.setSecure(False)
    cookie.setExpirationDate(websignin.QDateTime.currentDateTimeUtc().addDays(30))
    _, kept = websession.follow(sign_in_site.address, websignin.pack([cookie]))
    assert sign_in_site.asked == [('eu.account.battle.net', '/login/', 'login=known')]
    # And the window's browser reads what the plain request kept, with what the answer set.
    back = {bytes(cookie.name().data()): cookie for cookie in websignin.unpack(kept)}
    assert sorted(back) == [b'login', b'seen'] and bytes(back[b'seen'].value()) == b'again'
    assert back[b'seen'].domain() == 'eu.account.battle.net' and back[b'seen'].isHttpOnly()
    assert not back[b'seen'].isSessionCookie() and back[b'login'].domain() == '.battle.net'
